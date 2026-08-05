"""Abaqus batch processing — orchestrator, resource planner, and public helpers.

Key classes
-----------
AbaqusCalculation
	Thin assembly of JobContext + strategy; no side effects in ``__init__``.
BatchAbaqusProcessor
	Three-phase lifecycle: ``plan`` / ``prepare`` / ``run_batch``.
JobOutcome
	Unified result envelope for a single job.
"""

from __future__ import annotations

import copy
import logging
import math
import os
import shutil
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field

import psutil
from rich.progress import (
	BarColumn,
	Progress,
	SpinnerColumn,
	TextColumn,
	TimeElapsedColumn,
)

from .context import JobContext
from .registry import build_workflow
from .runner import AbaqusRunner, CommandRecord, _check_abqpy_installed
from .spec import JobSpec
from .status import JobStatus

# ======================== IMP-05: dry-run data model ========================

@dataclass
class JobPlan:
	"""Dry-run output for a single job — commands, paths, and resource summary.

	Attributes
	----------
	job_name : str
		Job identifier.
	commands : list
		List of :class:`CommandRecord` instances.
	paths : dict[str, str]
		Expected file paths (``inp``, ``odb``, ``output_dir``).
	resource_summary : dict
		Planned CPUs, token estimate, and parallelism info.
	"""
	job_name: str
	commands: list = field(default_factory=list)
	paths: dict = field(default_factory=dict)
	resource_summary: dict = field(default_factory=dict)


# ======================== AbaqusCalculation (thin wrapper) ========================

class AbaqusCalculation:
	"""Thin wrapper: assembles JobContext + AbaqusRunner, delegates to strategy.

	No side effects in ``__init__`` (only creates the output directory and
	builds the immutable JobContext).  The logger is created lazily on the
	first call to :meth:`execute`.

	Attributes
	----------
	job_name : str
		Unique job identifier.
	output_dir : str
		Working directory for this job.
	workflow_strategy : JobWorkflowStrategy
		The assembled workflow to execute.
	cpus_per_job : int
		Number of CPUs requested for the solver.
	abaqus_exe : str
		Path to the Abaqus executable.
	timeout : float or None
		Per-subprocess timeout in seconds.
	ctx : JobContext
		Immutable context built from the constructor arguments.
	"""

	def __init__(
		self,
		job_name: str,
		output_dir: str,
		workflow_strategy,
		cpus_per_job: int,
		abaqus_exe: str = 'abaqus',
		timeout: float | None = None,
		user_subroutine: str | None = None,
	):

		self.job_name = job_name
		self.output_dir = output_dir
		self.workflow_strategy = workflow_strategy
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.timeout = timeout
		self.user_subroutine = user_subroutine
		self.logger: logging.Logger | None = None

		# Build internals
		self.ctx = JobContext(
			job_name=job_name,
			output_dir=output_dir,
			cpus=cpus_per_job,
			abaqus_exe=abaqus_exe,
			user_subroutine=user_subroutine,
		)
		os.makedirs(output_dir, exist_ok=True)

	def execute(self, phase: str = 'full') -> dict:
		"""Run the workflow (or a single phase of it) and return the result dict.

		Creates the logger and the :class:`AbaqusRunner` on first call, then
		delegates to ``self.workflow_strategy.execute()`` (``phase='full'``)
		or to the matching ``<phase>_only`` method on the strategy.

		Parameters
		----------
		phase : str
			``'full'`` (default) runs the complete workflow. ``'prepare'``,
			``'simulate'``, or ``'extract'`` run only that phase via the
			strategy's ``prepare_only``/``simulate_only``/``extract_only``
			method (see :class:`~ABQflow.core.strategies.JobWorkflowStrategy`'s
			optional phase-separated protocol).

		Returns
		-------
		dict
			Must contain at least ``'status'``.  May include extracted values.

		Raises
		------
		NotImplementedError
			If ``phase != 'full'`` and ``self.workflow_strategy`` does not
			implement the corresponding ``<phase>_only`` method (e.g.
			:class:`~ABQflow.core.strategies.MonolithicWorkflowStrategy`).
		"""
		if self.logger is None:
			self.logger = self._setup_logging()
		self.logger.info(f"======== [AbaqusCalculation] Start Workflow ({phase}): {self.job_name} ========")
		runner = AbaqusRunner(self.ctx, self.logger, timeout=self.timeout)

		if phase == 'full':
			results = self.workflow_strategy.execute(self.ctx, runner, self.logger)
		else:
			method = getattr(self.workflow_strategy, f'{phase}_only', None)
			if method is None:
				raise NotImplementedError(
					f"{type(self.workflow_strategy).__name__} does not support "
					f"phase-separated execution ('{phase}_only' not implemented)."
				)
			outcome = method(self.ctx, runner, self.logger)
			results = outcome[0]  # (results, status_manager) or (results, status_manager, stop)

		self.logger.info(f"======== [AbaqusCalculation] Workflow Finished ({phase}): {self.job_name} ========")
		return results

	def _setup_logging(self) -> logging.Logger:
		logger = logging.getLogger(f"AbaqusCalculation_{self.job_name}")
		if logger.hasHandlers():
			logger.handlers.clear()
		logger.setLevel(logging.INFO)
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
		file_handler = logging.FileHandler(self.ctx.exec_log_path, encoding='utf-8')
		file_handler.setLevel(logging.DEBUG)
		file_handler.setFormatter(formatter)
		logger.addHandler(file_handler)
		return logger


# ======================== JobOutcome (fix Q2-1, Q2-4) ========================

@dataclass
class JobOutcome:
	"""Unified result envelope returned from every job, pass or fail.

	Status is normalised to a plain string (``JobStatus.value``) so it
	serialises cleanly across process boundaries.

	Attributes
	----------
	job_name : str
		Name of the job.
	status : str
		String status, e.g. ``"COMPLETED"`` or ``"SIMULATION_FAILED"``.
	results : dict or None
		Extracted result values, or ``None`` if the job did not reach
		extraction.
	error : str or None
		Error message if the job failed, ``None`` otherwise.
	diagnostics : dict or None
		Solver diagnostics snapshot (IMP-02).  Populated on failure and
		on the ``rc≠0 + COMPLETED`` edge case.  ``None`` for clean success
		or jobs that never reached the solver phase.
	phases : list[dict] or None
		Phase-by-phase history (name/status/duration/error) collected from
		:class:`~ABQflow.core.status.JobStatusManager`. ``None`` for
		strategies that don't populate it (e.g. monolithic workflows).
	duration_s : float or None
		Wall-clock seconds spent in :meth:`AbaqusCalculation.execute` for
		this job.
	"""
	job_name: str
	status: str
	results: dict | None = None
	error: str | None = None
	diagnostics: dict | None = None
	output_dir: str | None = None
	phases: list[dict] | None = None
	duration_s: float | None = None


# ======================== Resource planning (fix Q2-2) ========================

def solver_tokens(n_cpus: int) -> int:
	"""Estimate Abaqus license tokens needed for *n_cpus* cores.

	Formula: ``token(n) = ceil(5 * n^0.422)``, an empirical approximation
	of Abaqus licensing behaviour.

	Parameters
	----------
	n_cpus : int
		Number of CPU cores per job.

	Returns
	-------
	int
		Estimated token count.
	"""
	return math.ceil(5 * n_cpus ** 0.422)


def plan_parallelism(requested: int, cpus_per_job: int,
					license_tokens: int | None = None,
					reserve_cores: int = 1) -> int:
	"""Compute the actual number of concurrent jobs given hardware and license limits.

	Constraints applied in order:

	1. CPU cores available (``os.cpu_count() - reserve_cores``).
	2. License tokens (if provided).
	3. User-requested maximum.

	Parameters
	----------
	requested : int
		Desired number of parallel jobs.
	cpus_per_job : int
		CPUs each job will request.
	license_tokens : int or None
		Total license tokens available.  ``None`` means unconstrained.
	reserve_cores : int
		Cores to reserve for the OS and other processes (default 1).

	Returns
	-------
	int
		Feasible parallelism level (at least 1).
	"""
	# total = os.cpu_count() or 1
	total = psutil.cpu_count(logical=False)
	p_cpu = max(1, (total - reserve_cores) // cpus_per_job)
	p = min(requested, p_cpu)
	if license_tokens is not None:
		p = min(p, max(1, license_tokens // solver_tokens(cpus_per_job)))
	if p < requested:
		logging.getLogger('BatchAbaqusProcessor').warning(
			f"Parallelism reduced from {requested} to {p} "
			f"(CPU limit {p_cpu}, tokens per job {solver_tokens(cpus_per_job)})")
	return p


# ======================== Worker (fix B18, fix Q2-1) ========================

def _worker(calc: AbaqusCalculation, phase: str = 'full') -> JobOutcome:
	"""Top-level entry point for :class:`~concurrent.futures.ProcessPoolExecutor`.

	All exceptions are caught and wrapped in a :class:`JobOutcome` — they
	never propagate to the pool, so one failed job cannot crash the batch.

	Parameters
	----------
	calc : AbaqusCalculation
		Fully configured calculation to run.
	phase : str
		Forwarded to :meth:`AbaqusCalculation.execute` — ``'full'``,
		``'prepare'``, ``'simulate'``, or ``'extract'``.

	Returns
	-------
	JobOutcome
		Result envelope (status is always a plain string). ``phases`` and
		``duration_s`` are populated so the main process can log a
		phase-by-phase summary (see :meth:`BatchAbaqusProcessor._log_job_summary`).
	"""
	started_at = time.time()
	try:
		results = calc.execute(phase=phase)
		raw = results.pop('status', JobStatus.UNKNOWN)
		status = raw.value if isinstance(raw, JobStatus) else str(raw)
		# IMP-02: promote solver diagnostics from results to top-level field
		diag = results.pop('diagnostics', None)
		phases = results.pop('_phase_history', None)
		return JobOutcome(calc.job_name, status, results,
						diagnostics=diag, output_dir=calc.ctx.output_dir,
						phases=phases, duration_s=time.time() - started_at)
	except Exception as e:
		return JobOutcome(calc.job_name, JobStatus.UNKNOWN_ERROR.value,
						error=f"{type(e).__name__}: {e}",
						output_dir=calc.ctx.output_dir,
						duration_s=time.time() - started_at)


# ======================== BatchAbaqusProcessor ========================

class BatchAbaqusProcessor:
	"""Orchestrate a batch of Abaqus jobs through a three-phase lifecycle.

	1. :meth:`plan` — inspect for directory conflicts, compute decisions.
	   Pure computation; no side effects.
	2. :meth:`prepare` — apply decisions (delete, rename, skip) and build
	   the :class:`AbaqusCalculation` list.
	3. :meth:`run_batch` — execute via :class:`~concurrent.futures.ProcessPoolExecutor`;
	   one failure never affects sibling jobs.

	Attributes
	----------
	specs : list[JobSpec]
		Normalised list of job specifications.
	calculations : list[AbaqusCalculation] or None
		Built calculations (populated by :meth:`prepare`).
	logger : logging.Logger
		Logger writing to ``batch_processor.log`` in the output directory.
	"""

	def __init__(
		self,
		batch_data: list[dict] | list[JobSpec],
		base_output_dir: str,
		cpus_per_job: int,
		abaqus_exe: str = 'abaqus',
		duplicate_mode: str = 'fail',                # B12: 'fail' default, not 'interactive'
		prompt_fn = input,
		timeout: float | None = None,
		preflight_only: bool = False,
	):
		"""
		Parameters
		----------
		batch_data : list[dict] or list[JobSpec]
			Job configs as dicts or :class:`JobSpec` objects.  Dicts are
			converted via :meth:`JobSpec.from_dict`.
		base_output_dir : str
			**Absolute** directory where all job subdirectories will be created.
		cpus_per_job : int
			Number of CPUs to request for each Abaqus job.
		abaqus_exe : str
			**Absolute** path to the Abaqus executable (default ``'abaqus'``).
		duplicate_mode : str
			How to handle existing job directories (default ``'fail'``):

			* ``'fail'`` — raise :class:`FileExistsError` on any conflict.
			* ``'skip'`` — skip jobs whose directory already exists.
			* ``'overwrite'`` — delete the existing directory and re-run.
			* ``'interactive'`` — prompt the user for each conflict.
		prompt_fn : callable
			Function for interactive prompts (default :func:`input`).
		timeout : float or None
			Per-subprocess timeout in seconds; ``None`` means no limit.
		preflight_only : bool
			If ``True``, only run preparation + preflight, skip solver &
			extraction (IMP-04 batch inspection mode).
		"""
		self.base_output_dir = base_output_dir
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.duplicate_mode = duplicate_mode.lower()
		self._prompt = prompt_fn
		self.timeout = timeout
		self.preflight_only = preflight_only

		# Normalize: accept both dicts and JobSpecs
		if batch_data and isinstance(batch_data[0], JobSpec):
			self.specs: list[JobSpec] = batch_data
		else:
			self.specs = [JobSpec.from_dict(d) for d in batch_data]

		# Validate: no duplicate names (fix B14)
		names = [s.job_name for s in self.specs]
		dup = {n for n in names if names.count(n) > 1}
		if dup:
			raise ValueError(f"Duplicate job_name in batch: {sorted(dup)}")

		os.makedirs(base_output_dir, exist_ok=True)
		self._log_path = os.path.join(self.base_output_dir, 'batch_processor.log')
		self.logger = self._setup_logging()
		self.calculations: list[AbaqusCalculation] | None = None

	def _setup_logging(self) -> logging.Logger:
		logger = logging.getLogger('BatchAbaqusProcessor')
		if logger.hasHandlers():
			logger.handlers.clear()
		logger.setLevel(logging.INFO)
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
		file_handler = logging.FileHandler(self._log_path, mode='a', encoding='utf-8')
		file_handler.setFormatter(formatter)
		logger.addHandler(file_handler)
		logger.info("======== Batch Processor Start ========")
		logger.info(f"Duplicate mode: {self.duplicate_mode}")
		return logger


	# ---- IMP-05: dry_run ----

	def dry_run(self, level: str = 'plan') -> list[JobPlan]:
		"""Inspect what the batch would do without executing it.

		Two levels (see IMP-05):

		``'plan'`` (default)
			**Zero side effects.**  Inspects each spec and builds a command
			plan without touching the filesystem.
		``'stage'``
			Runs the real preparation phase, but substitutes a
			``record_only`` runner so solver and hook commands are
			logged, not executed.  **Has filesystem side effects**
			(``output_dir`` is created, INPs are staged).

		Parameters
		----------
		level : str
			``'plan'`` (L1, default) or ``'stage'`` (L2).

		Returns
		-------
		list[JobPlan]
			One plan per job.
		"""
		if level == 'plan':
			return self._dry_run_plan()
		elif level == 'stage':
			return self._dry_run_stage()
		else:
			raise ValueError(f"Unknown dry_run level: '{level}'. Use 'plan' or 'stage'.")

	def _dry_run_plan(self) -> list[JobPlan]:
		"""L1: zero-side-effect command plan from specs only.

		Reuses :class:`AbaqusRunner`'s pure command-builder static methods
		(``build_preflight_command`` / ``build_solver_command`` /
		``build_script_command``) instead of re-deriving the Abaqus CLI
		syntax here — the two paths would otherwise silently drift apart.
		Constructing a :class:`JobContext` has no side effects (no directory
		is created), so L1 stays a pure read of the specs.
		"""
		has_abqpy = _check_abqpy_installed()
		plans: list[JobPlan] = []
		for spec in self.specs:
			cmds: list = []
			out_dir = os.path.join(self.base_output_dir, spec.job_name)
			ctx = JobContext(job_name=spec.job_name, output_dir=out_dir,
							cpus=self.cpus_per_job, abaqus_exe=self.abaqus_exe,
							user_subroutine=spec.subroutine.source_path if spec.subroutine else None)

			# Subroutine compile command (modular only)
			if spec.subroutine and spec.workflow == 'modular' and not spec.subroutine.precompiled:
				cmds.append(CommandRecord(
					'compile', AbaqusRunner.build_make_command(ctx, spec.subroutine), out_dir))

			# Preflight command
			if spec.preflight:
				pf_cmd, _ = AbaqusRunner.build_preflight_command(ctx, spec.preflight)
				cmds.append(CommandRecord('preflight', pf_cmd, out_dir))

			# Solver command (modular only; monolithic handles its own)
			if spec.workflow == 'modular':
				cmds.append(CommandRecord('solver', AbaqusRunner.build_solver_command(ctx), out_dir))

			# Hook commands — pre-extraction needs the CAE kernel (mdb), like
			# ModelPropertiesExtractionStrategy; post-extraction only needs
			# odbAccess, like OdbExtractionStrategy.
			for hook in spec.pre_extraction or []:
				cmd = AbaqusRunner.build_script_command(
					hook.script_path, needs_cae_kernel=True,
					abaqus_exe=self.abaqus_exe, has_abqpy=has_abqpy)
				cmd += ['--job_name', spec.job_name, '--tasks_json', '<generated-at-runtime>']
				cmds.append(CommandRecord(f'hook:{hook.script_path}', cmd, out_dir))
			for hook in spec.post_extraction or []:
				cmd = AbaqusRunner.build_script_command(
					hook.script_path, needs_cae_kernel=False,
					abaqus_exe=self.abaqus_exe, has_abqpy=has_abqpy)
				cmd += ['--job_name', spec.job_name, '--tasks_json', '<generated-at-runtime>']
				cmds.append(CommandRecord(f'hook:{hook.script_path}', cmd, out_dir))

			# Monolithic workflow
			if spec.workflow == 'monolithic' and spec.monolithic_script:
				cmd = AbaqusRunner.build_script_command(
					spec.monolithic_script, needs_cae_kernel=True,
					abaqus_exe=self.abaqus_exe, has_abqpy=has_abqpy)
				cmds.append(CommandRecord('monolithic', cmd, out_dir))

			tokens_per = solver_tokens(self.cpus_per_job)
			plans.append(JobPlan(
				job_name=spec.job_name,
				commands=cmds,
				paths={'inp': ctx.inp_path, 'odb': ctx.odb_path, 'output_dir': out_dir},
				resource_summary={
					'cpus_per_job': self.cpus_per_job,
					'tokens_per_job': tokens_per,
					'timeout': self.timeout,
				},
			))
		return plans

	def _dry_run_stage(self) -> list[JobPlan]:
		"""L2: real preparation + record_only for solver/hooks."""
		# Reuse prepare() for real staging, but with record_only runner
		decisions = self.plan()
		calcs: list[AbaqusCalculation] = []
		for spec in self.specs:
			decision = decisions.get(spec.job_name, 'run')
			if decision == 'skip':
				continue
			if decision == 'overwrite':
				dirpath = os.path.join(self.base_output_dir, spec.job_name)
				shutil.rmtree(dirpath, ignore_errors=True)
			elif decision not in ('run', None):
				spec = copy.deepcopy(spec)
				spec.job_name = decision

			calcs.append(self._build_calc(spec))

		plans: list[JobPlan] = []
		for calc in calcs:
			# L2: execute with record_only runner
			calc.logger = calc._setup_logging()
			runner = AbaqusRunner(calc.ctx, calc.logger, timeout=calc.timeout, record_only=True)
			calc.workflow_strategy.execute(calc.ctx, runner, calc.logger)
			plans.append(JobPlan(
				job_name=calc.job_name,
				commands=runner.command_log,
				paths={'inp': calc.ctx.inp_path, 'odb': calc.ctx.odb_path,
					'output_dir': calc.output_dir},
				resource_summary={
					'cpus_per_job': calc.cpus_per_job,
					'tokens_per_job': solver_tokens(calc.cpus_per_job),
				},
			))
		return plans


	# ---- plan: pure computation, no side effects ----
	def plan(self) -> dict[str, str]:
		"""Inspect output directory for existing job subdirectories.

		Pure read-only check — no directories are created, deleted, or
		renamed.  The decision for each job is one of: ``'run'``,
		``'skip'``, ``'overwrite'``, or a new name string (rename).

		Returns
		-------
		dict[str, str]
			``{job_name: decision}`` mapping.

		Raises
		------
		FileExistsError
			If ``duplicate_mode='fail'`` and any job directory already exists.
		"""
		decisions: dict[str, str] = {}
		conflicts = [s for s in self.specs
					if os.path.isdir(os.path.join(self.base_output_dir, s.job_name))]

		if not conflicts:
			return {s.job_name: 'run' for s in self.specs}

		conflict_names = [s.job_name for s in conflicts]
		self.logger.warning(f"Existing job dirs: {conflict_names}")

		if self.duplicate_mode == 'fail':
			raise FileExistsError(
				f"Mode[fail] — existing jobs: {', '.join(conflict_names)}")
		if self.duplicate_mode == 'skip':
			for s in conflicts:
				decisions[s.job_name] = 'skip'
		elif self.duplicate_mode == 'overwrite':
			for s in conflicts:
				decisions[s.job_name] = 'overwrite'
		elif self.duplicate_mode == 'interactive':
			decisions.update(self._interactive_resolve(conflicts))
		else:
			raise ValueError(f"Unknown duplicate_mode: {self.duplicate_mode}")

		# Non-conflicting jobs → run
		for s in self.specs:
			if s.job_name not in decisions:
				decisions[s.job_name] = 'run'
		return decisions

	def _interactive_resolve(self, conflicts: list[JobSpec]) -> dict[str, str]:
		decisions: dict[str, str] = {}
		overwrite_all = skip_all = False
		for spec in conflicts:
			name = spec.job_name
			if overwrite_all:
				decisions[name] = 'overwrite'
				continue
			if skip_all:
				decisions[name] = 'skip'
				continue

			while True:
				resp = self._prompt(
					f"\n Job '{name}' exists:\n"
					f"  [o]verwrite  [s]kip  [r]ename  [O]verwrite All  [S]kip All  [A]bort\n"
					f"  >>> ").strip()
				if resp == 'o':
					decisions[name] = 'overwrite'
					break
				elif resp == 's':
					decisions[name] = 'skip'
					break
				elif resp == 'r':
					decisions[name] = self._find_available_name(name)
					break
				elif resp == 'O':
					overwrite_all = True
					decisions[name] = 'overwrite'
					break
				elif resp == 'S':
					skip_all = True
					decisions[name] = 'skip'
					break
				elif resp.lower() == 'a':
					raise RuntimeError("User aborted batch processing.")
		return decisions

	def _find_available_name(self, original: str) -> str:
		v = 2
		while True:
			n = f"{original}_v{v}"
			if not os.path.isdir(os.path.join(self.base_output_dir, n)):
				return n
			v += 1



	# ---- shared calculation builder (used by prepare(), dry-run, and the phase-only methods) ----
	def _build_calc(self, spec: JobSpec) -> AbaqusCalculation:
		"""Build an :class:`AbaqusCalculation` directly from *spec*.

		No conflict resolution (no overwrite/rename/skip, no interactive
		prompt) — ``output_dir`` is always deterministically derived from
		*spec* (``base_output_dir/spec.job_name``), reusing an existing
		directory in place if present. This is what lets
		:meth:`run_preparation`/:meth:`run_simulation`/:meth:`run_extraction`
		be called standalone (even in a fresh process/session): the caller
		just needs to reconstruct a :class:`BatchAbaqusProcessor` with the
		same ``batch_data``/``base_output_dir`` used before.
		"""
		workflow = build_workflow(spec, preflight_only=self.preflight_only)
		return AbaqusCalculation(
			job_name=spec.job_name,
			output_dir=os.path.join(self.base_output_dir, spec.job_name),
			workflow_strategy=workflow,
			cpus_per_job=self.cpus_per_job,
			abaqus_exe=self.abaqus_exe,
			timeout=self.timeout,
			user_subroutine=spec.subroutine.source_path if spec.subroutine else None,
		)

	# ---- prepare: apply decisions, build calculations ----
	def prepare(self, decisions: dict[str, str] | None = None):
		"""Apply plan decisions and build the :class:`AbaqusCalculation` list.

		Side effects: directories may be deleted (``'overwrite'``) or
		specs may be renamed (``'rename'``).  Results are stored in
		``self.calculations``.

		Parameters
		----------
		decisions : dict[str, str] or None
			Decision map from :meth:`plan`.  If ``None``, :meth:`plan` is
			called first.
		"""
		if decisions is None:
			decisions = self.plan()

		calcs = []
		for spec in self.specs:
			decision = decisions.get(spec.job_name, 'run')

			if decision == 'skip':
				self.logger.info(f"  - Skipping: {spec.job_name}")
				continue
			elif decision == 'overwrite':
				dirpath = os.path.join(self.base_output_dir, spec.job_name)
				self.logger.info(f"  - Overwriting: {spec.job_name}")
				shutil.rmtree(dirpath, ignore_errors=True)
			elif decision not in ('run', None):
				# decision is a new name
				self.logger.info(f"  - Renaming: {spec.job_name} -> {decision}")
				spec = copy.deepcopy(spec)
				spec.job_name = decision

			calcs.append(self._build_calc(spec))

		self.calculations = calcs
		self.logger.info(f"Prepared {len(calcs)} jobs.")

	# ---- batch log summary (lightweight aggregation) ----
	def _log_job_summary(self, oc: JobOutcome):
		"""Write a phase-by-phase summary of *oc* to ``batch_processor.log``.

		Called once per completed job, right after its
		:class:`~concurrent.futures.ProcessPoolExecutor` future resolves —
		a post-hoc block, not a live cross-process log stream. This is what
		makes preparation/simulation/extraction activity show up in the
		batch-level log, which previously stopped recording after
		:meth:`prepare`.
		"""
		self.logger.info(f"---- {oc.job_name}: {oc.status} ({oc.duration_s or 0:.1f}s) ----")
		for p in (oc.phases or []):
			err = f" — {p['error']}" if p.get('error') else ""
			self.logger.info(f"    [{p['phase']}] {p['status']} ({p.get('duration_s') or 0:.2f}s){err}")
		if oc.error and not oc.phases:
			self.logger.info(f"    error: {oc.error}")

	# ---- shared ProcessPoolExecutor driver ----
	def _execute_pool(
		self,
		calcs: list[AbaqusCalculation],
		phase: str,
		num_parallel_jobs: int,
		license_tokens: int | None = None,
	) -> list[JobOutcome]:
		"""Run *calcs* through :class:`~concurrent.futures.ProcessPoolExecutor`.

		Shared by :meth:`run_batch` and the phase-only methods
		(:meth:`run_preparation`/:meth:`run_simulation`/:meth:`run_extraction`)
		so progress reporting and batch-log summarization
		(:meth:`_log_job_summary`) apply uniformly to all of them.

		Parameters
		----------
		calcs : list[AbaqusCalculation]
			Calculations to execute.
		phase : str
			Forwarded to :func:`_worker` / :meth:`AbaqusCalculation.execute`
			— ``'full'``, ``'prepare'``, ``'simulate'``, or ``'extract'``.
		num_parallel_jobs : int
			Desired maximum concurrent jobs.
		license_tokens : int or None
			Total license tokens available; ``None`` means no license limit.

		Returns
		-------
		list[JobOutcome]
			One outcome per executed job.  Failed jobs are included with
			their error state — they do not halt the batch.
		"""
		p = plan_parallelism(num_parallel_jobs, self.cpus_per_job, license_tokens)
		outcomes: list[JobOutcome] = []

		progress_columns = [
			SpinnerColumn(),
			TextColumn("[progress.description]{task.description}", justify="right"),
			BarColumn(),
			TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
			TextColumn("({task.completed} of {task.total})"),
			TimeElapsedColumn(),
		]

		with Progress(*progress_columns) as progress, \
			ProcessPoolExecutor(max_workers=p) as pool:
			task = progress.add_task(f"[bold blue]Running ({phase})...", total=len(calcs))
			futures = {pool.submit(_worker, c, phase): c.job_name for c in calcs}

			for fut in as_completed(futures):
				try:
					oc = fut.result()
				except Exception as e:
					oc = JobOutcome(futures[fut], JobStatus.UNKNOWN_ERROR.value,
									error=str(e))
				outcomes.append(oc)
				self._log_job_summary(oc)
				icon = "✅" if oc.status == "COMPLETED" else "❌"
				progress.update(task, advance=1,
								description=f"{icon} {oc.job_name} ({oc.status})")

		return outcomes

	# ---- run_batch: full pipeline (all calculations, all phases) ----
	def run_batch(
		self,
		num_parallel_jobs: int,
		license_tokens: int | None = None
	) -> list[JobOutcome]:
		"""Execute all prepared calculations via :class:`~concurrent.futures.ProcessPoolExecutor`.

		If :meth:`prepare` has not been called yet it is invoked with a
		fresh call to :meth:`plan`.

		Parameters
		----------
		num_parallel_jobs : int
			Desired maximum concurrent jobs.
		license_tokens : int or None
			Total license tokens available; ``None`` means no license limit.

		Returns
		-------
		list[JobOutcome]
			One outcome per executed job.  Failed jobs are included with
			their error state — they do not halt the batch.
		"""
		if self.calculations is None:
			self.prepare(self.plan())
		return self._execute_pool(self.calculations, 'full', num_parallel_jobs, license_tokens)

	# ---- phase-separated batch methods ----
	def run_preparation(
		self, num_parallel_jobs: int = 1, license_tokens: int | None = None
	) -> list[JobOutcome]:
		"""Run only the preparation phase (produce INPs, and compile a
		subroutine if configured) for every spec in ``self.specs``.

		Builds :class:`AbaqusCalculation`\\ s directly from ``self.specs``
		(see :meth:`_build_calc`) rather than through :meth:`plan`/
		:meth:`prepare`'s duplicate-directory handling — call :meth:`plan`/
		:meth:`prepare` first if you need overwrite/rename/skip semantics.
		Only ``workflow='modular'`` specs are supported; a ``monolithic``
		spec raises :class:`NotImplementedError` (it has no separable
		preparation phase).

		Parameters
		----------
		num_parallel_jobs : int
			Desired maximum concurrent jobs (default ``1``).
		license_tokens : int or None
			Total license tokens available; ``None`` means no license limit.

		Returns
		-------
		list[JobOutcome]
			One outcome per job.
		"""
		calcs = [self._build_calc(spec) for spec in self.specs]
		return self._execute_pool(calcs, 'prepare', num_parallel_jobs, license_tokens)

	def run_simulation(
		self, num_parallel_jobs: int = 1, license_tokens: int | None = None
	) -> list[JobOutcome]:
		"""Run only the solver phase (pre-extraction + solve) for every spec.

		Assumes ``ctx.inp_path`` already exists for every job (e.g. from a
		prior :meth:`run_preparation` call — possibly in an earlier
		process/session; reconstruct the :class:`BatchAbaqusProcessor` with
		the same ``batch_data``/``base_output_dir`` to resume). Only
		``workflow='modular'`` specs are supported.

		Parameters
		----------
		num_parallel_jobs : int
			Desired maximum concurrent jobs (default ``1``).
		license_tokens : int or None
			Total license tokens available; ``None`` means no license limit.

		Returns
		-------
		list[JobOutcome]
			One outcome per job.
		"""
		calcs = [self._build_calc(spec) for spec in self.specs]
		return self._execute_pool(calcs, 'simulate', num_parallel_jobs, license_tokens)

	def run_extraction(
		self, num_parallel_jobs: int = 1, license_tokens: int | None = None
	) -> list[JobOutcome]:
		"""Run only the post-extraction phase for every spec.

		Assumes ``ctx.odb_path`` already exists for every job (e.g. from a
		prior :meth:`run_simulation` call). Only ``workflow='modular'``
		specs are supported.

		Parameters
		----------
		num_parallel_jobs : int
			Desired maximum concurrent jobs (default ``1``).
		license_tokens : int or None
			Total license tokens available; ``None`` means no license limit.

		Returns
		-------
		list[JobOutcome]
			One outcome per job.
		"""
		calcs = [self._build_calc(spec) for spec in self.specs]
		return self._execute_pool(calcs, 'extract', num_parallel_jobs, license_tokens)
