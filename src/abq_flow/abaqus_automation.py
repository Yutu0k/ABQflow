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
import re
import shutil
import warnings
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass

import numpy as np
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from .context import JobContext
from .registry import build_workflow
from .runner import AbaqusRunner
from .spec import JobSpec
from .status import JobStatus


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
	):

		self.job_name = job_name
		self.output_dir = output_dir
		self.workflow_strategy = workflow_strategy
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.timeout = timeout
		self.logger: logging.Logger | None = None

		# Build internals
		self.ctx = JobContext(
			job_name=job_name,
			output_dir=output_dir,
			cpus=cpus_per_job,
			abaqus_exe=abaqus_exe,
		)
		os.makedirs(output_dir, exist_ok=True)

	def execute(self) -> dict:
		"""Run the workflow and return the result dict.

		Creates the logger and the :class:`AbaqusRunner` on first call, then
		delegates to ``self.workflow_strategy.execute()``.

		Returns
		-------
		dict
			Must contain at least ``'status'``.  May include extracted values.
		"""
		if self.logger is None:
			self.logger = self._setup_logging()
		self.logger.info(f"======== [AbaqusCalculation] Start Workflow: {self.job_name} ========")
		runner = AbaqusRunner(self.ctx, self.logger, timeout=self.timeout)
		results = self.workflow_strategy.execute(self.ctx, runner, self.logger)
		self.logger.info(f"======== [AbaqusCalculation] Workflow Finished: {self.job_name} ========")
		return results

	def __init__(
		self,
		job_name: str,
		output_dir: str,
		workflow_strategy,
		cpus_per_job: int,
		abaqus_exe: str = 'abaqus',
		timeout: float | None = None,
	):
		self.job_name = job_name
		self.output_dir = output_dir
		self.workflow_strategy = workflow_strategy
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.timeout = timeout
		self.logger: logging.Logger | None = None

		# Build internals
		self.ctx = JobContext(
			job_name=job_name,
			output_dir=output_dir,
			cpus=cpus_per_job,
			abaqus_exe=abaqus_exe,
		)
		os.makedirs(output_dir, exist_ok=True)

	def execute(self) -> dict:
		if self.logger is None:
			self.logger = self._setup_logging()
		self.logger.info(f"======== [AbaqusCalculation] Start Workflow: {self.job_name} ========")
		runner = AbaqusRunner(self.ctx, self.logger, timeout=self.timeout)
		results = self.workflow_strategy.execute(self.ctx, runner, self.logger)
		self.logger.info(f"======== [AbaqusCalculation] Workflow Finished: {self.job_name} ========")
		return results

	def _setup_logging(self) -> logging.Logger:
		logger = logging.getLogger(f"AbaqusCalculation_{self.job_name}")
		if logger.hasHandlers():
			logger.handlers.clear()
		logger.setLevel(logging.INFO)
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
		file_handler = logging.FileHandler(self.ctx.log_path, encoding='utf-8')
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
	"""
	job_name: str
	status: str
	results: dict | None = None
	error: str | None = None


# ======================== Resource planning (fix Q2-2) ========================

def solver_tokens(n_cpus: int) -> int:
	"""Estimate Abaqus license tokens needed for *n_cpus* cores.

	Formula: ``token(n) = ceil(5 * n^0.422)``, an empirical approximation
	of Abaqus licensing behaviour.

	Args
	----
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

	Args
	----
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
	total = os.cpu_count() or 1
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

def _worker(calc: AbaqusCalculation) -> JobOutcome:
	"""Top-level entry point for :class:`~concurrent.futures.ProcessPoolExecutor`.

	All exceptions are caught and wrapped in a :class:`JobOutcome` — they
	never propagate to the pool, so one failed job cannot crash the batch.

	Args
	----
	calc : AbaqusCalculation
		Fully configured calculation to run.

	Returns
	-------
	JobOutcome
		Result envelope (status is always a plain string).
	"""
	try:
		results = calc.execute()
		raw = results.pop('status', JobStatus.UNKNOWN)
		status = raw.value if isinstance(raw, JobStatus) else str(raw)
		return JobOutcome(calc.job_name, status, results)
	except Exception as e:
		return JobOutcome(calc.job_name, JobStatus.UNKNOWN_ERROR.value,
						error=f"{type(e).__name__}: {e}")


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
	):
		"""
		Args
		----
		batch_data : list[dict] or list[JobSpec]
			Job configs as dicts or :class:`JobSpec` objects.  Dicts are
			converted via :meth:`JobSpec.from_dict`.
		base_output_dir : str
			Directory where all job subdirectories will be created.
		cpus_per_job : int
			Number of CPUs to request for each Abaqus job.
		abaqus_exe : str
			Path to the Abaqus executable (default ``'abaqus'``).
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
		"""
		self.base_output_dir = base_output_dir
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.duplicate_mode = duplicate_mode.lower()
		self._prompt = prompt_fn
		self.timeout = timeout

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



	# ---- prepare: apply decisions, build calculations ----
	def prepare(self, decisions: dict[str, str] | None = None):
		"""Apply plan decisions and build the :class:`AbaqusCalculation` list.

		Side effects: directories may be deleted (``'overwrite'``) or
		specs may be renamed (``'rename'``).  Results are stored in
		``self.calculations``.

		Args
		----
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

			workflow = build_workflow(spec)
			calc = AbaqusCalculation(
				job_name=spec.job_name,
				output_dir=os.path.join(self.base_output_dir, spec.job_name),
				workflow_strategy=workflow,
				cpus_per_job=self.cpus_per_job,
				abaqus_exe=self.abaqus_exe,
				timeout=self.timeout,
			)
			calcs.append(calc)

		self.calculations = calcs
		self.logger.info(f"Prepared {len(calcs)} jobs.")

	# ---- run_batch: ProcessPoolExecutor + fault-tolerant collection ----
	def run_batch(
		self,
		num_parallel_jobs: int,
		license_tokens: int | None = None
	) -> list[JobOutcome]:
		"""Execute all prepared calculations via :class:`~concurrent.futures.ProcessPoolExecutor`.

		If :meth:`prepare` has not been called yet it is invoked with a
		fresh call to :meth:`plan`.

		Args
		----
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
			task = progress.add_task("[bold blue]Running...", total=len(self.calculations))
			futures = {pool.submit(_worker, c): c.job_name for c in self.calculations}

			for fut in as_completed(futures):
				try:
					oc = fut.result()
				except Exception as e:
					oc = JobOutcome(futures[fut], JobStatus.UNKNOWN_ERROR.value,
									error=str(e))
				outcomes.append(oc)
				icon = "✅" if oc.status == "COMPLETED" else "❌"
				progress.update(task, advance=1,
								description=f"{icon} {oc.job_name} ({oc.status})")

		return outcomes


# ======================== Result conversion ========================

def outcomes_to_list(outcomes: list[JobOutcome]) -> list[dict]:
	"""Convert a list of :class:`JobOutcome` objects to a list of plain dicts.

	Convenience for callers that prefer the legacy list-of-dicts shape.

	Args
	----
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.

	Returns
	-------
	list[dict]
		Each dict contains ``'job_name'``, ``'status'``, flattened results,
		and optionally ``'error'``.
	"""
	out = []
	for oc in outcomes:
		d = {**(oc.results or {}), 'status': oc.status, 'job_name': oc.job_name}
		if oc.error:
			d['error'] = oc.error
		out.append(d)
	return out


def outcomes_to_dict(outcomes: list[JobOutcome]) -> dict[str, dict]:
	"""Convert a list of :class:`JobOutcome` objects to a ``{job_name: {...}}`` dict.

	Args
	----
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.

	Returns
	-------
	dict[str, dict]
		Each value dict contains ``'status'``, flattened results, and
		optionally ``'error'``.

	Raises
	------
	ValueError
		If two outcomes share the same ``job_name``.
	"""
	out = {}
	for oc in outcomes:
		if oc.job_name in out:
			raise ValueError(f"Duplicate job_name in dict output: {oc.job_name}")
		d = {**(oc.results or {}), 'status': oc.status}
		if oc.error:
			d['error'] = oc.error
		out[oc.job_name] = d
	return out


# ======================== Array generation / degeneration ========================

def generate_from_array(samples_array, param_names, base_spec) -> list[JobSpec]:
	"""Create N :class:`JobSpec` objects from an (N, D) parameter array.

	Each row of *samples_array* becomes a new spec via :func:`copy.deepcopy`
	of *base_spec*, so every spec owns independent mutable state.

	Args
	----
	samples_array : ndarray or Tensor
		Shape ``(N, D)`` parameter matrix.  Torch tensors are converted to
		NumPy internally.
	param_names : list[str]
		Length-D list of parameter names.
	base_spec : JobSpec or dict
		Template spec.  Dicts are upgraded via :meth:`JobSpec.from_dict`.

	Returns
	-------
	list[JobSpec]
		N specs with zero-padded names (e.g. ``job_0001``, ``job_0002``).

	Raises
	------
	ValueError
		If the array column count does not match ``len(param_names)``.
	"""
	if hasattr(samples_array, 'numpy'):
		samples_array = samples_array.numpy()

	n, d = samples_array.shape
	if d != len(param_names):
		raise ValueError(f"Dimension mismatch: array has {d} cols, param_names has {len(param_names)}")

	# Accept both JobSpec and dict
	if not isinstance(base_spec, JobSpec):
		base_spec = JobSpec.from_dict(base_spec)

	specs = []
	for i in range(n):
		s = copy.deepcopy(base_spec)                                         # fix shallow copy
		s.job_name = f"{base_spec.job_name}_{i+1:04d}"
		params = {k: float(v) for k, v in zip(param_names, samples_array[i, :].tolist())}
		if s.workflow == 'monolithic':
			s.monolithic_params = params
		else:
			if s.preparation is not None:
				s.preparation.params = params
		specs.append(s)
	return specs


def _natural_key(name: str):
	"""Split *name* into (text, int, text, ...) tuples for natural sort order.

	Ensures ``job_2`` sorts before ``job_10``.
	"""
	return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def degenerate_from_array(outcomes: list[JobOutcome], output_names: list[str],
						default_value=np.nan, require_completed: bool = True) -> np.ndarray:
	"""Extract a 2D NumPy array of output values from a list of outcomes.

	Outcomes are sorted by natural key on ``job_name`` so rows appear in
	the order the jobs were generated.  Jobs that are not ``COMPLETED`` are
	filled with *default_value* and trigger a warning.

	Args
	----
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.
	output_names : list[str]
		Keys to extract from each outcome's ``results`` dict.
	default_value : float
		Value to use for missing or non-completed results (default ``NaN``).
	require_completed : bool
		If ``True`` (default), warn when non-``COMPLETED`` jobs are
		encountered.

	Returns
	-------
	np.ndarray
		Shape ``(len(outcomes), len(output_names))`` float array.
	"""
	# Sort by natural key on job_name
	sorted_outcomes = sorted(outcomes, key=lambda o: _natural_key(o.job_name))

	rows = []
	bad = []
	for oc in sorted_outcomes:
		if require_completed and oc.status != "COMPLETED":
			bad.append(oc.job_name)
		r = oc.results or {}
		rows.append([r.get(n, default_value) for n in output_names])

	if bad:
		warnings.warn(f"{len(bad)} jobs not COMPLETED, rows contain default values: {bad}")

	return np.asarray(rows, dtype=float)
