"""Abaqus batch processing — refactored architecture.

Thin AbaqusCalculation wrapper, ProcessPoolExecutor + JobOutcome for async (fix Q2),
plan/prepare split (fix B12), resource planning, JobSpec pipeline.
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
	"""
	Thin wrapper: assembles JobContext + AbaqusRunner, delegates to strategy.

	No side effects in __init__ (fix B12). Logger is lazy.

	Methods
	-------
	execute() -> dict
		使用workflow_strategy跑一个任务(`workflow_strategy.execute()`), return results dict (status + outputs).



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
	"""Unified result envelope. Status is always a string (JSON-serializable)."""
	job_name: str
	status: str          # JobStatus.value string, e.g. "COMPLETED"
	results: dict | None = None
	error: str | None = None


# ======================== Resource planning (fix Q2-2) ========================

def solver_tokens(n_cpus: int) -> int:
	"""Abaqus license token formula: T(n) = ceil(5 * n^0.422)."""
	return math.ceil(5 * n_cpus ** 0.422)


def plan_parallelism(requested: int, cpus_per_job: int,
					license_tokens: int | None = None,
					reserve_cores: int = 1) -> int:
	"""Compute actual parallelism from CPU and license constraints."""
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
	"""Top-level function for ProcessPoolExecutor. Exception → JobOutcome, never propagates."""
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
	"""Orchestrate batch Abaqus jobs. plan/prepare/run_batch split (fix B12).
	
	Methods
	-------
	plan() -> dict[str, str]
		Plan job execution: {job_name: 'run'|'skip'|'overwrite'|new_name}. No directories touched.
	prepare(decisions: dict[str, str] | None = None)
		Apply plan decisions (rmtree, rename) and build AbaqusCalculation list.
	run_batch(num_parallel_jobs: int, license_tokens: int | None = None) -> list[JobOutcome]
		Execute all calculations. One failure does not affect others (fix Q2-1).
	
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
		Parameters
		----------
		batch_data: list[dict] | list[JobSpec]
			JobSpec dicts or JobSpec objects. Each spec is a single Abaqus job.
		base_output_dir: str
			Directory where all job subdirectories will be created.
		cpus_per_job: int
			Number of CPUs to request for each Abaqus job.
		abaqus_exe: str, default = 'abaqus'
			Path to the Abaqus executable.
		duplicate_mode: str, default = 'fail'
			How to handle existing job directories. Options:
			- 'fail': raise an error if any job directory exists.
			- 'skip': skip existing jobs.
			- 'overwrite': delete existing job directories and run.
			- 'interactive': prompt the user for each conflict.
		prompt_fn: callable, default = input
			Function to call for user input in interactive mode. Should accept a prompt string and return a string.
		timeout: float | None, default = None
			Timeout in seconds for each subprocess call. None means no timeout.
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
		"""
		针对batch_data中的每个JobSpec, 检查base_output_dir下是否存在同名目录

		Return {job_name: 'run'|'skip'|'overwrite'|new_name}. No directories touched.
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
		"""Apply plan decisions (rmtree, rename) and build AbaqusCalculation list."""
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
		"""Execute all calculations. One failure does not affect others (fix Q2-1)."""
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
	"""Convert outcomes to old list-of-dicts format."""
	out = []
	for oc in outcomes:
		d = {**(oc.results or {}), 'status': oc.status, 'job_name': oc.job_name}
		if oc.error:
			d['error'] = oc.error
		out.append(d)
	return out


def outcomes_to_dict(outcomes: list[JobOutcome]) -> dict[str, dict]:
	"""Convert outcomes to {job_name: {...}} format. Raises on duplicate names (fix B14/B15)."""
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
	"""Generate JobSpecs from a parameter array. Deep-copies base_spec (fix Q3 shallow copy).

	Args:
		samples_array: shape (N, D) — numpy array or torch tensor.
		param_names: list of D strings.
		base_spec: JobSpec or dict (compat). Each row overrides preparation/monolithic params.

	Returns:
		list[JobSpec]: N specs with zero-padded names (e.g. job_0001).
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
	"""Natural sort: job_2 < job_10."""
	return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def degenerate_from_array(outcomes: list[JobOutcome], output_names: list[str],
						default_value=np.nan, require_completed: bool = True) -> np.ndarray:
	"""Extract a 2D array from outcomes. Natural sort, status-aware (fix B16)."""
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
