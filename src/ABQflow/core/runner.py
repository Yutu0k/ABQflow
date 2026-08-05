"""AbaqusRunner — subprocess gateway that encapsulates every shell call a strategy needs.

Provides environment detection (abqpy / CAE kernel / odbAccess), sentinel-based
JSON extraction, timeout-safe command execution, solver diagnostics, and a
``record_only`` dry-run mode (IMP-05).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass, field

from ..helpers.constant import RESULT_BEGIN, RESULT_END
from .context import JobContext
from .diagnostics import SolverDiagnostics, SolverResult, apply_truth_table, diagnose
from .spec import SubroutineSpec

# ---------------------------------------------------------------------------
# Path to hookkit.py (staged into job output dir so hooks can import it)
# ---------------------------------------------------------------------------
_HOOKKIT_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'hookkit.py')
_SIDECAR_KEY = '__file__'

# ---------------------------------------------------------------------------
# IMP-03: escalation-ladder constants
# ---------------------------------------------------------------------------
_GRACE_MIN = 30    # minimum grace period for terminate to write ODB (s)
_GRACE_MAX = 300   # maximum grace period (s) — beyond this terminate is stuck

# ---------------------------------------------------------------------------
# IMP-05: dry-run data model
# ---------------------------------------------------------------------------

@dataclass
class CommandRecord:
	"""One command that was (or would be) executed."""
	stage: str        # 'preflight' | 'solver' | 'hook:<script>' | 'preparation'
	cmd: list[str]
	cwd: str

def _check_abqpy_installed() -> bool:
	"""Return ``True`` if the ``abqpy`` package is importable."""
	try:
		import abqpy  # noqa: F401
		return True
	except ImportError:
		return False


def extract_json(text: str) -> dict:
	"""Extract a JSON object from subprocess stdout.

	Protocol: the script wraps its JSON payload between sentinel markers
	``===ABQ_RESULT_BEGIN===`` and ``===ABQ_RESULT_END===``.  If both are
	present the payload between them is parsed directly.  Otherwise falls
	back to a legacy brace-scan that searches from the *end* of the output
	(useful when Abaqus prints a banner before user code runs).

	Parameters
	----------
	text : str
		Raw stdout captured from a subprocess call.

	Returns
	-------
	dict
		Parsed JSON payload.

	Raises
	------
	ValueError
		If no JSON object can be found or parsed.
	"""
	if RESULT_BEGIN in text and RESULT_END in text:
		payload = text.split(RESULT_BEGIN, 1)[1].split(RESULT_END, 1)[0]
		return json.loads(payload)
	return _legacy_brace_scan(text)

def _legacy_brace_scan(text: str) -> dict:
	"""Scan from the *end* for the last complete JSON object (Abaqus banner is at the front)."""
	# Find last '{' and try to parse balanced braces from there
	last_brace = text.rfind('{')
	if last_brace == -1:
		raise ValueError("No '{' found in output.")
	candidate = text[last_brace:]
	try:
		return json.loads(candidate)
	except json.JSONDecodeError as e:
		try:
			return json.loads(candidate[:e.pos])
		except json.JSONDecodeError:
			raise ValueError(f"Failed to parse JSON from output: '{candidate[:100]}...'")


class AbaqusRunner:
	"""Encapsulates every subprocess call a strategy may need.

	Detects the execution environment and routes commands accordingly:

	* **abqpy installed** — uses plain ``python`` (abqpy wraps the Abaqus API).
	* **Needs CAE kernel** (``mdb``) — uses ``abaqus cae noGUI=<script>``.
	* **Only needs odbAccess** — uses ``abaqus python <script>``.

	Attributes
	----------
	ctx : JobContext
		Frozen context providing job name, paths, CPU count, and Abaqus exe.
	logger : logging.Logger
		Logger instance for this runner.
	timeout : float or None
		Per-command timeout in seconds; ``None`` means no limit.
	"""

	def __init__(self, ctx: JobContext, logger: logging.Logger,
				timeout: float | None = None, record_only: bool = False):
		self.ctx = ctx
		self.logger = logger
		self.timeout = timeout
		self.record_only = record_only
		self.command_log: list[CommandRecord] = []
		self._has_abqpy = _check_abqpy_installed()

	# ---- IMP-03: terminate escalation ladder helpers ----

	def _grace_period(self) -> int:
		"""Compute the grace period G = clamp(0.05 × T, 30, 300) seconds."""
		if self.timeout is None:
			return _GRACE_MAX
		return max(_GRACE_MIN, min(int(0.05 * self.timeout), _GRACE_MAX))

	def _terminate_abaqus_job(self):
		"""Level 1: send ``abaqus terminate job=<name>`` for graceful shutdown."""
		cmd = [self.ctx.abaqus_exe, 'terminate', f'job={self.ctx.job_name}']
		self.logger.warning(f"Escalation level 1: {' '.join(cmd)}")
		try:
			subprocess.run(cmd, capture_output=True, text=True, timeout=30)
		except Exception as e:
			self.logger.warning(f"terminate command failed: {e}")

	def _kill_process_tree(self, pid: int):
		"""Level 3: force-kill the entire process tree.

		Uses ``taskkill /T`` on Windows, ``os.killpg`` on POSIX.
		"""
		self.logger.warning(f"Escalation level 3: killing process tree of PID {pid}")
		try:
			if sys.platform == 'win32':
				subprocess.run(
					['taskkill', '/T', '/F', '/PID', str(pid)],
					capture_output=True, timeout=15,
				)
			else:
				import signal
				os.killpg(pid, signal.SIGKILL)  # ponytail: SIGKILL is nuclear but correct here
		except Exception as e:
			self.logger.error(f"Force-kill failed: {e}")

	def _cleanup_lck(self):
		"""Level 4: remove ``<job>.lck`` so the job can be re-run."""
		lck = os.path.join(self.ctx.output_dir, f"{self.ctx.job_name}.lck")
		if os.path.exists(lck):
			self.logger.warning(f"Escalation level 4: removing {lck}")
			try:
				os.remove(lck)
			except OSError as e:
				self.logger.error(f"Failed to remove .lck: {e}")

	# ---- hookkit staging (HK-01 §3.5) ----
	def _stage_hookkit(self):
		"""Copy ``hookkit.py`` into the job output dir so hooks can ``import hookkit``.

		Uses content-hash comparison: if an identical file already exists the
		copy is skipped (re-run safe).  The file is NOT deleted afterwards —
		it is a reproducible artifact of the job run.
		"""
		if not os.path.isfile(_HOOKKIT_SRC):
			self.logger.warning("hookkit.py not found at %s — hooks using hookkit will fail", _HOOKKIT_SRC)
			return

		dst = os.path.join(self.ctx.output_dir, 'hookkit.py')
		if os.path.isfile(dst):
			with open(_HOOKKIT_SRC, 'rb') as f:
				src_hash = hashlib.sha256(f.read()).hexdigest()
			with open(dst, 'rb') as f:
				dst_hash = hashlib.sha256(f.read()).hexdigest()
			if src_hash == dst_hash:
				return  # already present and identical
		shutil.copy2(_HOOKKIT_SRC, dst)

	# ---- envelope validation (HK-01 §3.6) ----
	@staticmethod
	def _validate_envelope(value: dict, output_dir: str, logger: logging.Logger) -> dict | None:
		"""Validate a sidecar envelope and return an enriched copy, or ``None``.

		Steps (in order):
		1. Path safety — reject ``../`` escapes and absolute paths.
		2. Existence — reject missing or zero-byte files.
		3. Metadata augmentation — fill missing ``columns`` / ``shape``;
		   if claimed ``shape`` differs from file, overwrite + warn.
		"""
		if not isinstance(value, dict):
			return value  # not a sidecar

		file_name = value.get(_SIDECAR_KEY)
		if not file_name:
			return value  # not a sidecar

		# 1. Path safety
		abs_path = os.path.normpath(os.path.join(output_dir, file_name))
		if not abs_path.startswith(os.path.normpath(output_dir) + os.sep):
			logger.warning(
				"Sidecar path escape rejected: '%s' → result set to None", file_name
			)
			return None

		# 2. Existence
		if not os.path.isfile(abs_path) or os.path.getsize(abs_path) == 0:
			logger.warning(
				"Sidecar file missing or empty: '%s' → result set to None", abs_path
			)
			return None

		# 3. Metadata augmentation — file is authoritative
		import csv as _csv
		enriched = dict(value)

		try:
			with open(abs_path, 'r', newline='') as f:
				reader = _csv.reader(f)
				header = next(reader)
				actual_rows = sum(1 for _ in reader)
		except Exception as e:
			logger.warning("Cannot read sidecar CSV '%s': %s → result set to None", abs_path, e)
			return None

		actual_n_cols = len(header)

		# Warn if claimed shape differs from reality (envelope-lying detection)
		claimed_shape = enriched.get('shape')
		if claimed_shape is not None:
			if claimed_shape[0] != actual_rows or claimed_shape[1] != actual_n_cols:
				logger.warning(
					"Sidecar shape mismatch: claimed %s, file has [%d, %d] — using file",
					claimed_shape, actual_rows, actual_n_cols,
				)

		enriched['columns'] = header
		enriched['shape'] = [actual_rows, actual_n_cols]

		return enriched

	# ---- Execution environment selection (fix B5/B6/B11) ----
	@staticmethod
	def build_script_command(script: str, needs_cae_kernel: bool,
							abaqus_exe: str, has_abqpy: bool) -> list[str]:
		"""Select the correct interpreter and Abaqus entry-point for *script*.

		Pure function — no instance state required — so both the real
		execution path (:meth:`_base_command`) and dry-run planning
		(:meth:`~abaqus_batch_pack.abaqus_automation.BatchAbaqusProcessor._dry_run_plan`)
		can share one definition instead of maintaining separate copies.

		Decision logic (first match wins):

		1. ``abqpy`` available — ``['python', script]``.
		2. ``needs_cae_kernel`` is True — ``[exe, 'cae', 'noGUI=<script>', '--']``.
		   The ``'--'`` separator prevents custom args from being consumed by the
		   Abaqus CLI.
		3. Otherwise — ``[exe, 'python', script]`` (``odbAccess``-only scripts).

		Parameters
		----------
		script : str
			Path to the Python script to execute.
		needs_cae_kernel : bool
			Whether the script requires the CAE kernel (``mdb`` access).
		abaqus_exe : str
			Path or command name for the Abaqus executable.
		has_abqpy : bool
			Whether the ``abqpy`` package is importable in this environment.

		Returns
		-------
		list[str]
			Command line as a list of tokens ready for ``subprocess.run``.
		"""
		if has_abqpy:
			return ['python', script]
		if needs_cae_kernel:
			return [abaqus_exe, 'cae', f'noGUI={script}', '--']
		return [abaqus_exe, 'python', script]

	def _base_command(self, script: str, needs_cae_kernel: bool) -> list[str]:
		"""Instance-bound convenience wrapper around :meth:`build_script_command`."""
		return self.build_script_command(script, needs_cae_kernel,
										self.ctx.abaqus_exe, self._has_abqpy)

	@staticmethod
	def build_solver_command(ctx: JobContext) -> list[str]:
		"""Build the ``abaqus job=... input=... cpus=... [user=...] interactive`` command line.

		Pure function of *ctx* — shared by :meth:`run_solver` and dry-run
		planning so the two never drift apart. ``user=<ctx.user_subroutine>``
		is inserted (before ``interactive``) when a subroutine is configured.
		"""
		cmd = [ctx.abaqus_exe, f'job={ctx.job_name}',
				f'input={ctx.inp_path}', f'cpus={ctx.cpus}']
		if ctx.user_subroutine:
			cmd.append(f'user={ctx.user_subroutine}')
		cmd.append('interactive')
		return cmd

	@staticmethod
	def build_preflight_command(ctx: JobContext, mode: str) -> tuple[list[str], str]:
		"""Build the ``abaqus <mode> job=<job>_chk input=... [user=...]`` command line.

		Returns
		-------
		tuple[list[str], str]
			``(cmd, chk_name)`` — *chk_name* is the temporary job name used
			so preflight output never overwrites the real job's files.
		"""
		chk_name = f"{ctx.job_name}_chk"
		cmd = [ctx.abaqus_exe, mode, f'job={chk_name}', f'input={ctx.inp_path}']
		if ctx.user_subroutine:
			cmd.append(f'user={ctx.user_subroutine}')
		if mode == 'datacheck':
			cmd.append('cpus=1')
		return cmd, chk_name

	@staticmethod
	def build_make_command(ctx: JobContext, subroutine: SubroutineSpec) -> list[str]:
		"""Build the ``abaqus make library=<source> [explicit|cfd]`` command line.

		Pure function shared by :meth:`run_compile` and dry-run planning.
		``solver='standard'`` needs no extra flag; ``'explicit'``/``'cfd'``
		are appended as bare flags — mirrors the convention documented in
		``reference/abaqus-cli`` (verify against the installed Abaqus
		version before relying on this in production).
		"""
		cmd = [ctx.abaqus_exe, 'make', f'library={subroutine.source_path}']
		if subroutine.solver in ('explicit', 'cfd'):
			cmd.append(subroutine.solver)
		return cmd

	def run_solver(self) -> SolverResult:
		"""Submit the INP file to the Abaqus solver and wait for completion.

		Uses :class:`~subprocess.Popen` with process-group isolation so that
		the terminate escalation ladder can reach solver child processes
		(``standard.exe`` / ``explicit.exe``) — something ``subprocess.run``
		cannot do.

		Escalation ladder (IMP-03):

		0. Normal wait up to ``self.timeout``.
		1. Graceful: ``abaqus terminate job=<name>``.
		2. Grace period G = clamp(0.05 × T, 30, 300) s.
		3. Force-kill the process tree (``taskkill /T`` or ``os.killpg``).
		4. Remove ``<job>.lck`` so the job can be re-run.

		After the solver process exits (by any means), :func:`diagnose` is
		called and the truth table applied.

		Returns
		-------
		SolverResult
			Success/failure judgment with diagnostics.
		"""
		cmd = self.build_solver_command(self.ctx)

		if self.record_only:
			self.command_log.append(CommandRecord('solver', cmd, self.ctx.output_dir))
			self.logger.info(f"[record_only] would run: {' '.join(cmd)}")
			return SolverResult(success=True, diagnostics=SolverDiagnostics())

		# ---- launch with process-group isolation ----
		popts: dict = {'cwd': self.ctx.output_dir}
		if sys.platform == 'win32':
			popts['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
		else:
			popts['start_new_session'] = True
		# Discard console stdout/stderr — solver writes its own .log/.sta/.msg/.dat
		popts['stdout'] = subprocess.DEVNULL
		popts['stderr'] = subprocess.DEVNULL

		try:
			proc = subprocess.Popen(cmd, **popts)
		except Exception as e:
			self.logger.error(f"Solver launch failed: {e}")
			diag = diagnose(self.ctx.job_name, self.ctx.output_dir)
			return SolverResult(success=False, error=str(e), diagnostics=diag)

		# ---- escalation ladder ----
		returncode = None
		escalation_level = 0
		T = self.timeout

		try:
			if T is not None:
				proc.wait(timeout=T)
				returncode = proc.returncode
			else:
				proc.wait()
				returncode = proc.returncode
		except subprocess.TimeoutExpired:
			# Level 1: graceful terminate
			escalation_level = 1
			self._terminate_abaqus_job()

			# Level 2: grace period
			G = self._grace_period()
			try:
				proc.wait(timeout=G)
				returncode = proc.returncode
			except subprocess.TimeoutExpired:
				# Level 3: force kill
				escalation_level = 3
				self._kill_process_tree(proc.pid)
				try:
					proc.wait(timeout=10)
				except subprocess.TimeoutExpired:
					proc.kill()
					proc.wait()
				returncode = None

			# Level 4: .lck cleanup (always, regardless of whether force-kill was needed)
			self._cleanup_lck()

		# ---- diagnose and apply truth table ----
		diag = diagnose(self.ctx.job_name, self.ctx.output_dir)

		if returncode is not None and returncode >= 0:
			success, warning = apply_truth_table(returncode, diag.sta_verdict)
		else:
			success, warning = False, None

		# Error message
		if success:
			error_msg = warning  # only populated for rc≠0+COMPLETED edge case
		else:
			if diag.errors:
				error_msg = diag.errors[0]
			elif escalation_level > 0:
				error_msg = (
					f"Timeout after {T}s, "
					f"terminated via escalation ladder (level {escalation_level})"
				)
			else:
				error_msg = (
					f"Abaqus exited with rc={returncode}, "
					f".sta verdict={diag.sta_verdict}"
				)

		return SolverResult(success=success, error=error_msg, diagnostics=diag)

	# ---- IMP-04: preflight ----

	def run_preflight(self, mode: str) -> tuple[bool, list[str]]:
		"""Run an Abaqus syntax/datacheck on the INP before the real solve.

		Uses a temporary job name ``<job>_chk`` so preflight output files
		(``.dat``, ``.odb``) never overwrite the real job's files.

		Parameters
		----------
		mode : str
			``'syntaxcheck'`` or ``'datacheck'``.

		Returns
		-------
		tuple[bool, list[str]]
			``(passed, errors)`` — *errors* are harvested from the temporary
			``.dat`` file via :func:`harvest_errors` (IMP-01/04 synergy).
		"""
		cmd, chk_name = self.build_preflight_command(self.ctx, mode)

		if self.record_only:
			self.command_log.append(CommandRecord('preflight', cmd, self.ctx.output_dir))
			self.logger.info(f"[record_only] would run: {' '.join(cmd)}")
			return (True, [])

		self.logger.info(f"Preflight [{mode}]: {' '.join(cmd)}")
		returncode = None
		try:
			proc = subprocess.run(
				cmd, cwd=self.ctx.output_dir,
				capture_output=True, text=True, timeout=self.timeout or 300,
			)
			returncode = proc.returncode
		except subprocess.TimeoutExpired:
			self.logger.error(f"Preflight [{mode}] timed out")
			returncode = None
		except Exception as e:
			self.logger.error(f"Preflight [{mode}] launch failed: {e}")
			return (False, [str(e)])

		# Harvest errors from the temporary .dat file
		from .diagnostics import harvest_errors
		chk_dat = os.path.join(self.ctx.output_dir, f"{chk_name}.dat")
		errors, _, _ = harvest_errors(chk_dat, None) if os.path.isfile(chk_dat) else ([], 0, 0)

		# Cleanup temporary preflight files
		for ext in ('.dat', '.msg', '.sta', '.log', '.odb', '.com', '.prt', '.lck',
					'.sim', '.par', '.pes', '.abq', '.mdl', '.stt', '.023'):
			tmpf = os.path.join(self.ctx.output_dir, f"{chk_name}{ext}")
			if os.path.isfile(tmpf):
				try:
					os.remove(tmpf)
				except OSError:
					pass

		passed = (returncode == 0) and (len(errors) == 0)
		return (passed, errors)

	# ---- user subroutine compilation ----

	def _compile_hash_path(self, subroutine: SubroutineSpec) -> str:
		stem = os.path.splitext(os.path.basename(subroutine.source_path))[0]
		return os.path.join(self.ctx.output_dir, f".{stem}.compiled.sha256")

	def subroutine_needs_recompile(self, subroutine: SubroutineSpec) -> bool:
		"""Return ``True`` if *subroutine* has changed since the last successful compile.

		Compares the sha256 of ``subroutine.source_path`` against a sidecar
		hash file written by :meth:`_record_compile_hash` after a successful
		compile (same hash-compare-and-skip pattern as :meth:`_stage_hookkit`).
		Always ``True`` if no prior compile record exists.
		"""
		hash_path = self._compile_hash_path(subroutine)
		if not os.path.isfile(hash_path):
			return True
		try:
			with open(subroutine.source_path, 'rb') as f:
				current_hash = hashlib.sha256(f.read()).hexdigest()
			with open(hash_path, 'r') as f:
				recorded_hash = f.read().strip()
		except OSError:
			return True
		return current_hash != recorded_hash

	def _record_compile_hash(self, subroutine: SubroutineSpec):
		"""Write the sidecar hash file marking *subroutine* as freshly compiled."""
		with open(subroutine.source_path, 'rb') as f:
			current_hash = hashlib.sha256(f.read()).hexdigest()
		with open(self._compile_hash_path(subroutine), 'w') as f:
			f.write(current_hash)

	def run_compile(self, subroutine: SubroutineSpec) -> tuple[bool, str, str]:
		"""Run ``abaqus make`` to compile *subroutine*.

		No regex parsing of compiler errors is performed — stdout/stderr are
		captured and returned as-is for the caller to log (matches the
		reference tool's approach: compiler-error classification is left to
		a human/LLM reading the raw output, not this library).

		Parameters
		----------
		subroutine : SubroutineSpec
			Subroutine to compile.

		Returns
		-------
		tuple[bool, str, str]
			``(success, stdout, stderr)``.
		"""
		cmd = self.build_make_command(self.ctx, subroutine)

		if self.record_only:
			self.command_log.append(CommandRecord('compile', cmd, self.ctx.output_dir))
			self.logger.info(f"[record_only] would run: {' '.join(cmd)}")
			return (True, '', '')

		self.logger.info(f"Compile subroutine: {' '.join(cmd)}")
		try:
			proc = subprocess.run(
				cmd, cwd=self.ctx.output_dir,
				capture_output=True, text=True, timeout=self.timeout or 600,
			)
		except subprocess.TimeoutExpired:
			msg = f"Compile timed out ({self.timeout or 600}s): {' '.join(cmd)}"
			self.logger.error(msg)
			return (False, '', msg)
		except Exception as e:
			self.logger.error(f"Compile launch failed: {e}")
			return (False, '', str(e))

		if proc.returncode != 0:
			self.logger.error(f"Compile failed (rc={proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
			return (False, proc.stdout, proc.stderr)

		return (True, proc.stdout, proc.stderr)

	def run_hook(
		self,
		script_path: str,
		tasks: list[dict],
		common_args: dict[str, str],
		needs_cae_kernel: bool
	) -> dict:
		"""Execute a hook script with a JSON task list, return per-task results.

		Writes tasks to a temporary JSON file, launches the script via
		:meth:`_base_command` (so the correct environment is used), appends
		``common_args``, ``--job_name``, and ``--tasks_json``, then extracts
		the JSON result payload from stdout.

		Before execution, :meth:`_stage_hookkit` copies ``hookkit.py`` into
		the job output directory so hooks can ``import hookkit``.

		After execution, every sidecar envelope in the results dict passes
		through :meth:`_validate_envelope` for path-safety, existence, and
		metadata-augmentation checks.

		Parameters
		----------
		script_path : str
			Path to the hook script.
		tasks : list[dict]
			List of task descriptors, each expected to contain a
			``result_name`` key.
		common_args : dict[str, str]
			Extra CLI arguments forwarded to every task (e.g. ``--odb_path``).
		needs_cae_kernel : bool
			Passed through to :meth:`_base_command` for environment selection.

		Returns
		-------
		dict
			Mapping ``{result_name: value, ...}``.  Tasks that could not run
			map to ``None``.  Returns an empty dict when ``tasks`` is empty.
		"""
		if not tasks:
			return {}

		script_path = os.path.abspath(script_path)

		if self.record_only:
			cmd = self._base_command(script_path, needs_cae_kernel)
			for k, v in common_args.items():
				cmd += [k, str(v)]
			cmd += ['--job_name', self.ctx.job_name]
			cmd += ['--tasks_json', '<generated-at-runtime>']
			self.command_log.append(CommandRecord(f'hook:{script_path}', cmd, self.ctx.output_dir))
			self.logger.info(f"[record_only] would run hook: {' '.join(cmd)}")
			return {t['result_name']: None for t in tasks}

		# Stage hookkit into the job output dir (HK-01 §3.5)
		self._stage_hookkit()

		tmp = os.path.join(self.ctx.output_dir, f"tasks_{uuid.uuid4().hex}.json")
		try:
			with open(tmp, 'w', encoding='utf-8') as f:
				json.dump(tasks, f)

			cmd = self._base_command(script_path, needs_cae_kernel)
			for k, v in common_args.items():
				cmd += [k, str(v)]
			cmd += ['--job_name', self.ctx.job_name]
			cmd += ['--tasks_json', tmp]

			proc = self._run(cmd)
			if proc is None:
				return {t['result_name']: None for t in tasks}

			results = extract_json(proc.stdout)

			# Validate sidecar envelopes (HK-01 §3.6)
			for name, value in list(results.items()):
				if isinstance(value, dict) and _SIDECAR_KEY in value:
					validated = self._validate_envelope(value, self.ctx.output_dir, self.logger)
					results[name] = validated

			return results
		finally:
			if os.path.exists(tmp):
				# os.remove(tmp)
				pass

	def _run(self, cmd: list[str], cwd: str | None = None, stage: str = 'hook'):
		"""Execute *cmd* via ``subprocess.run``, capturing all output.

		This is the single subprocess entry point every strategy should use
		(directly or via :meth:`run_hook`) so that ``timeout``, error logging,
		and ``record_only`` dry-run behavior are applied uniformly — no
		strategy should call ``subprocess.run`` on its own.

		Timeout behavior: if ``self.timeout`` is set and the process exceeds
		it, a ``TimeoutExpired`` exception is caught, logged, and ``None`` is
		returned.  ``CalledProcessError`` is also caught and logged.

		Parameters
		----------
		cmd : list[str]
			Command tokens to execute.
		cwd : str or None
			Working directory.  Defaults to ``self.ctx.output_dir``.
		stage : str
			Label recorded on the :class:`CommandRecord` in ``record_only``
			mode (e.g. ``'preparation'``, ``'monolithic'``, ``'hook'``).

		Returns
		-------
		subprocess.CompletedProcess or None
			Completed process on success, ``None`` on timeout or non-zero exit.
		"""
		if self.record_only:
			self.command_log.append(CommandRecord(stage, cmd, cwd or self.ctx.output_dir))
			self.logger.info(f"[record_only] would run: {' '.join(cmd)}")
			# Return a fake success — caller checks for None
			# ponytail: fake CompletedProcess; use a real one only if a caller
			# accesses .returncode / .stdout beyond the current usage pattern
			class _FakeProc:
				returncode = 0
				stdout = '{}'
			return _FakeProc()

		try:
			return subprocess.run(
				cmd,
				cwd=cwd or self.ctx.output_dir,
				check=True,
				capture_output=True,
				text=True,
				timeout=self.timeout
			)
		except subprocess.TimeoutExpired:
			self.logger.error(f"Timeout ({self.timeout}s): {' '.join(cmd)}")
			return None
		except subprocess.CalledProcessError as e:
			self.logger.error(f"Command failed: {' '.join(cmd)}\n"
							f"STDERR:\n{e.stderr}\nSTDOUT:\n{e.stdout}")
			return None
