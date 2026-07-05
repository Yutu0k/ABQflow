"""AbaqusRunner — subprocess gateway that encapsulates every shell call a strategy needs.

Provides environment detection (abqpy / CAE kernel / odbAccess), sentinel-based
JSON extraction, and timeout-safe command execution.
"""

import json
import os
import uuid
import subprocess
import logging

from .context import JobContext
from .utils.helpers import check_abqpy_installed

# ---- Sentinel-based JSON extraction (fix B7/B13) ----
RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END = "===ABQ_RESULT_END==="


def extract_json(text: str) -> dict:
	"""Extract a JSON object from subprocess stdout.

	Protocol: the script wraps its JSON payload between sentinel markers
	``===ABQ_RESULT_BEGIN===`` and ``===ABQ_RESULT_END===``.  If both are
	present the payload between them is parsed directly.  Otherwise falls
	back to a legacy brace-scan that searches from the *end* of the output
	(useful when Abaqus prints a banner before user code runs).

	Args
	----
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
				timeout: float | None = None):
		self.ctx = ctx
		self.logger = logger
		self.timeout = timeout
		self._has_abqpy = check_abqpy_installed()

	# ---- Execution environment selection (fix B5/B6/B11) ----
	def _base_command(self, script: str, needs_cae_kernel: bool) -> list[str]:
		"""Select the correct interpreter and Abaqus entry-point for *script*.

		Decision logic (first match wins):

		1. ``abqpy`` available — ``['python', script]``.
		2. ``needs_cae_kernel`` is True — ``[exe, 'cae', 'noGUI=<script>', '--']``.
		   The ``'--'`` separator prevents custom args from being consumed by the
		   Abaqus CLI.
		3. Otherwise — ``[exe, 'python', script]`` (``odbAccess``-only scripts).

		Args
		----
		script : str
			Path to the Python script to execute.
		needs_cae_kernel : bool
			Whether the script requires the CAE kernel (``mdb`` access).

		Returns
		-------
		list[str]
			Command line as a list of tokens ready for ``subprocess.run``.
		"""
		if self._has_abqpy:
			return ['python', script]
		if needs_cae_kernel:
			return [self.ctx.abaqus_exe, 'cae', f'noGUI={script}', '--']
		return [self.ctx.abaqus_exe, 'python', script]

	def run_solver(self) -> bool:
		"""Submit the INP file to the Abaqus solver and wait for completion.

		Runs ``abaqus job=<name> input=<inp> cpus=<N> interactive``.

		Returns
		-------
		bool
			``True`` if the solver exited successfully, ``False`` on failure
			(including timeout).
		"""
		cmd = [self.ctx.abaqus_exe, f'job={self.ctx.job_name}', f'input={self.ctx.inp_path}', f'cpus={self.ctx.cpus}', 'interactive']
		return self._run(cmd, cwd=self.ctx.output_dir) is not None

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
		``common_args`` and ``--tasks_json``, then extracts the JSON result
		payload from stdout.

		Args
		----
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

		tmp = os.path.join(self.ctx.output_dir, f"tasks_{uuid.uuid4().hex}.json")
		try:
			with open(tmp, 'w', encoding='utf-8') as f:
				json.dump(tasks, f)

			cmd = self._base_command(script_path, needs_cae_kernel)
			for k, v in common_args.items():
				cmd += [k, str(v)]
			cmd += ['--tasks_json', tmp]

			proc = self._run(cmd)
			if proc is None:
				return {t['result_name']: None for t in tasks}
			return extract_json(proc.stdout)
		finally:
			if os.path.exists(tmp):
				os.remove(tmp)

	def _run(self, cmd: list[str], cwd: str | None = None):
		"""Execute *cmd* via ``subprocess.run``, capturing all output.

		Timeout behavior: if ``self.timeout`` is set and the process exceeds
		it, a ``TimeoutExpired`` exception is caught, logged, and ``None`` is
		returned.  ``CalledProcessError`` is also caught and logged.

		Args
		----
		cmd : list[str]
			Command tokens to execute.
		cwd : str or None
			Working directory.  Defaults to ``self.ctx.output_dir``.

		Returns
		-------
		subprocess.CompletedProcess or None
			Completed process on success, ``None`` on timeout or non-zero exit.
		"""
		try:
			return subprocess.run(cmd, cwd=cwd or self.ctx.output_dir, check=True, capture_output=True, text=True, timeout=self.timeout)
		except subprocess.TimeoutExpired:
			self.logger.error(f"Timeout ({self.timeout}s): {' '.join(cmd)}")
			return None
		except subprocess.CalledProcessError as e:
			self.logger.error(f"Command failed: {' '.join(cmd)}\n"
							f"STDERR:\n{e.stderr}\nSTDOUT:\n{e.stdout}")
			return None
