"""Job workflow strategies — the ABC hierarchy and all concrete implementations.

Strategies are stateless (configuration only in ``__init__``) and depend on
three injected arguments at call time: :class:`~abaqus_batch_pack.context.JobContext`,
:class:`~abaqus_batch_pack.runner.AbaqusRunner`, and ``logging.Logger``.
"""

from __future__ import annotations
import json
import logging
import os
import re
import subprocess
from abc import ABC, abstractmethod
from typing import List

from .context import JobContext
from .runner import AbaqusRunner, extract_json
from .diagnostics import SolverResult
from .status import JobStatus, JobStatusManager

# Regex for {{placeholder}} in INP files (B8)
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")

# Regex for *INCLUDE, INPUT=... lines — captures prefix + filename for rewriting
_INCLUDE_RE = re.compile(r'(\*INCLUDE\s*,\s*INPUT\s*=\s*)(\S+)', re.IGNORECASE)


# ======================== Preparation Strategies ========================
class PreparationStrategy(ABC):
	"""Interface for preparation: produce an INP file at ``ctx.inp_path``.

	Subclasses
	----------
	InpModifyStrategy
		Template-based INP generation (``{{placeholder}}`` substitution).
	ModelGenerationStrategy
		Run an external script that produces the INP (requires CAE kernel).
	"""

	@abstractmethod
	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		"""Produce the INP file.

		Parameters
		----------
		ctx : JobContext
			Job context providing ``inp_path`` and ``output_dir``.
		runner : AbaqusRunner
			Subprocess runner (may not be used by every strategy).
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		bool
			``True`` if the INP was produced, ``False`` otherwise.
		"""
		...


class InpModifyStrategy(PreparationStrategy):
	"""Replace ``{{placeholder}}`` tokens in a base INP template file.

	Performs coverage validation: if the INP references a placeholder that
	is missing from *data_params*, preparation fails.  If *data_params*
	contains keys that are not used in the INP, a warning is emitted.

	Attributes
	----------
	base_inp_path : str
		Path to the template INP file containing ``{{key}}`` placeholders.
	data_params : dict
		Mapping of placeholder names to substitution values.
	"""

	def __init__(self, base_inp_path: str, data_params: dict):
		self.base_inp_path = base_inp_path
		self.data_params = data_params

	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		logger.info(f"Sub strategy [InpModify]: Based on INP file '{self.base_inp_path}'")
		try:
			with open(self.base_inp_path, 'r') as f:
				content = f.read()
		except Exception as e:
			logger.error(f"Sub strategy [InpModify] failed reading INP: {e}")
			return False

		# B8: detect missing/unused placeholders
		found = set(_PLACEHOLDER_RE.findall(content))
		given = set(map(str, self.data_params.keys()))
		if missing := found - given:
			logger.error(f"INP placeholders missing parameters: {missing}")
			return False
		if unused := given - found:
			logger.warning(f"Parameters not used in INP: {unused}")

		content = _PLACEHOLDER_RE.sub(
			lambda m: str(self.data_params[m.group(1)]), content)

		with open(ctx.inp_path, 'w') as f:
			f.write(content)
		logger.info(f"Successfully created INP file: {ctx.inp_path}")
		return True


class ModelGenerationStrategy(PreparationStrategy):
	"""Run a model-generation script (requires CAE kernel / ``mdb`` access).

	The script is launched via ``abaqus cae noGUI=<script>`` and is expected
	to produce an INP file at ``ctx.inp_path``.  Common arguments
	(``--job_name``, user params) are forwarded as CLI flags.

	Attributes
	----------
	model_script_path : str
		Path to the model-generation script.
	script_params : dict
		Key-value pairs forwarded as ``--key value`` arguments.
	"""

	def __init__(self, model_script_path: str, script_params: dict):
		self.model_script_path = model_script_path
		self.script_params = script_params

	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		logger.info(f"Sub Strategy [ModelGeneration]: Run script '{self.model_script_path}'")
		# Model generation needs CAE kernel (mdb) → needs_cae_kernel=True (B6 fix)
		cmd = runner._base_command(self.model_script_path, needs_cae_kernel=True)
		for key, value in self.script_params.items():
			cmd.extend([f'--{key}', str(value)])
		cmd.extend(['--job_name', ctx.job_name])
		try:
			subprocess.run(cmd, check=True, capture_output=True, text=True,
						cwd=ctx.output_dir, timeout=runner.timeout)
			logger.info("Successfully generated model.")
			return os.path.exists(ctx.inp_path)
		except subprocess.CalledProcessError as e:
			logger.error(f"Sub Strategy [ModelGeneration] failed. STDERR:\n{e.stderr}")
			return False


class ExistingInpStrategy(PreparationStrategy):
	"""Use a pre-existing INP file directly — no generation or modification.

	This strategy satisfies the preparation contract ("ensure an INP at
	``ctx.inp_path``") by copying an already-complete INP file.  It is the
	entry point for the UC-03 "pre-existing INP batch" use case.

	Key features beyond a plain file copy:

	* **INCLUDE resolution**: scans for ``*INCLUDE, INPUT=...`` lines and
	  rewrites relative paths to absolute paths so Abaqus can find referenced
	  files regardless of the working directory.
	* **Template detection**: rejects INPs that still contain ``{{...}}``
	  placeholders, steering the user toward ``kind='inp_based'`` instead.
	* **STEP presence check**: confirms the file contains at least one
	  ``*STEP`` keyword.

	Attributes
	----------
	source_inp_path : str
		Absolute or relative path to the existing INP file.
	staging_mode : str
		``'copy'`` (default) — copy the INP (with resolved paths) to output_dir.
	resolve_includes : bool
		If ``True`` (default), rewrite ``*INCLUDE, INPUT=rel_path`` to use
		absolute paths resolved against the source INP's directory.
	"""

	def __init__(
		self,
		source_inp_path: str,
		staging_mode: str = 'copy',
		resolve_includes: bool = True,
	):
		self.source_inp_path = source_inp_path
		self.staging_mode = staging_mode
		self.resolve_includes = resolve_includes

	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		logger.info(f"Sub strategy [ExistingInp]: Using pre-existing INP '{self.source_inp_path}'")

		# 1. Existence & readability check
		if not os.path.isfile(self.source_inp_path):
			logger.error(f"Source INP not found: {self.source_inp_path}")
			return False

		# 2. Read content
		try:
			with open(self.source_inp_path, 'r') as f:
				content = f.read()
		except Exception as e:
			logger.error(f"Failed to read source INP: {e}")
			return False

		# 3. Lightweight content checks
		if not re.search(r'^\*STEP', content, re.MULTILINE | re.IGNORECASE):
			logger.error("INP contains no *STEP — not a valid Abaqus input file")
			return False

		if _PLACEHOLDER_RE.search(content):
			logger.error(
				"INP contains {{placeholder}} markers — this looks like a template, "
				"not a finished INP. Use kind='inp_based' with params instead."
			)
			return False

		# 4. Resolve *INCLUDE paths to absolute paths
		if self.resolve_includes:
			source_dir = os.path.dirname(os.path.abspath(self.source_inp_path))

			def _resolve_include(m: re.Match) -> str:
				prefix, rel_path = m.group(1), m.group(2)
				abs_path = os.path.normpath(os.path.join(source_dir, rel_path))
				if not os.path.isfile(abs_path):
					raise FileNotFoundError(abs_path)
				logger.info(f"  Resolved INCLUDE: {rel_path} -> {abs_path}")
				return f"{prefix}{abs_path}"

			try:
				content = _INCLUDE_RE.sub(_resolve_include, content)
			except FileNotFoundError as e:
				logger.error(f"INCLUDE file not found: {e}")
				return False

		# 5. Write the (possibly rewritten) INP to ctx.inp_path
		if self.staging_mode == 'copy':
			try:
				with open(ctx.inp_path, 'w') as f:
					f.write(content)
				logger.info(f"Wrote INP to {ctx.inp_path}")
			except Exception as e:
				logger.error(f"Failed to write INP: {e}")
				return False
		else:
			# ponytail: link/in_place deferred (S5), copy covers all MVP needs
			logger.error(f"Unsupported staging_mode: '{self.staging_mode}'. Only 'copy' is implemented.")
			return False

		return True


# ======================== Extraction Strategies ========================
class ExtractionStrategy(ABC):
	"""Interface for extraction: read results from model or ODB files.

	Subclasses
	----------
	OdbExtractionStrategy
		Post-simulation extraction from ODB (requires ``odbAccess``).
	ModelPropertiesExtractionStrategy
		Pre-simulation extraction from INP (requires ``mdb`` / CAE kernel).
	"""

	@abstractmethod
	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Extract results.

		Parameters
		----------
		ctx : JobContext
			Job context providing file paths.
		runner : AbaqusRunner
			Subprocess runner for launching hook scripts.
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		dict
			``{result_name: value, ...}``.  Failed tasks map to ``None``.
		"""
		...


class OdbExtractionStrategy(ExtractionStrategy):
	"""Extract results from the ODB file via hook scripts.

	Runs in the ``odbAccess`` environment (``abaqus python``), NOT the CAE
	kernel.  Each hook script receives ``--odb_path`` as a common argument
	and a JSON task list via ``--tasks_json``.

	Attributes
	----------
	hooks : list[dict]
		List of hook descriptors, each with ``script_path`` and ``tasks``.
	"""

	def __init__(self, hooks: list[dict]):
		self.hooks = hooks

	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Sub strategy [OdbExtract]: Start extracting from ODB...")

		if not os.path.exists(ctx.odb_path):
			logger.error(f"ODB file does not exist: {ctx.odb_path}")
			all_results = {}
			for hook in self.hooks:
				for task in hook['tasks']:
					all_results[task['result_name']] = None
			return all_results

		all_results = {}
		for hook in self.hooks:
			script_path = hook['script_path']
			tasks = hook['tasks']
			logger.info(f"  -> Run ODB hook script: {script_path} ({len(tasks)} tasks)")
			results = runner.run_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--odb_path': ctx.odb_path},
				needs_cae_kernel=False)   # odbAccess, not mdb
			all_results.update(results)
		return all_results


class ModelPropertiesExtractionStrategy(ExtractionStrategy):
	"""Extract material/property data from the INP *before* simulation.

	Runs in the CAE kernel environment (``abaqus cae noGUI``) because it
	needs ``mdb`` access.  Each hook script receives ``--inp_path`` as a
	common argument and a JSON task list via ``--tasks_json``.

	Attributes
	----------
	hooks : list[dict]
		List of hook descriptors, each with ``script_path`` and ``tasks``.
	"""

	def __init__(self, hooks: list[dict]):
		self.hooks = hooks

	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Sub strategy [ModelPropsExtract]: Start extracting from INP...")

		if not os.path.exists(ctx.inp_path):
			logger.error(f"INP file does not exist: {ctx.inp_path}")
			all_results = {}
			for hook in self.hooks:
				for task in hook['tasks']:
					all_results[task['result_name']] = None
			return all_results

		all_results = {}
		for hook in self.hooks:
			script_path = hook['script_path']
			tasks = hook['tasks']
			logger.info(f"  -> Run model property hook script: {script_path} ({len(tasks)} tasks)")
			results = runner.run_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--inp_path': ctx.inp_path},
				needs_cae_kernel=True)    # needs mdb
			all_results.update(results)
		return all_results


# ======================== Workflow Strategies ========================

class JobWorkflowStrategy(ABC):
	"""Interface for a complete job workflow.

	Subclasses
	----------
	MonolithicWorkflowStrategy
		Single-script workflow that handles everything itself.
	ModularWorkflowStrategy
		4-phase pipeline: preparation, pre-extraction, simulation,
		post-extraction.
	"""

	@abstractmethod
	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Run the full workflow and return a result dict.

		Parameters
		----------
		ctx : JobContext
			Job context.
		runner : AbaqusRunner
			Subprocess runner for all subprocess calls.
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		dict
			Must contain at least a ``'status'`` key (a :class:`JobStatus`
			or its string value).  May include extracted results.
		"""
		...


class MonolithicWorkflowStrategy(JobWorkflowStrategy):
	"""Single-script workflow: one script does everything.

	The script is launched via the CAE kernel (``abaqus cae noGUI``) and
	must print its JSON results wrapped in the sentinel markers
	``===ABQ_RESULT_BEGIN===`` / ``===ABQ_RESULT_END===``.  The result dict
	is expected to contain at least a ``'status'`` key.

	Attributes
	----------
	script_path : str
		Path to the monolithic script.
	params : dict
		Key-value parameters forwarded as ``--key value`` CLI arguments.
	"""

	def __init__(self, script_path: str, params: dict):
		self.script_path = script_path
		self.params = params

	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info(f"Workflow [MonolithicWorkflow]: Run script '{self.script_path}'")
		# B5/B6 fix: monolithic scripts use CAE kernel (mdb), not 'abaqus python'
		cmd = runner._base_command(self.script_path, needs_cae_kernel=True)
		for key, value in self.params.items():
			cmd.extend([f'--{key}', str(value)])

		try:
			proc = subprocess.run(cmd, check=True, capture_output=True, text=True,
								cwd=ctx.output_dir, timeout=runner.timeout)
			results = extract_json(proc.stdout)              # B7: sentinel-based extraction
			if 'status' not in results:
				results['status'] = JobStatus.COMPLETED
			logger.info("Monolithic script run successfully.")
			return results
		except subprocess.CalledProcessError as e:
			logger.error(f"Monolithic script failed. STDERR:\n{e.stderr}")  # B9: correct message
			return {'status': JobStatus.MONOLITHIC_SCRIPT_FAILED, 'error': e.stderr}
		except (ValueError, json.JSONDecodeError) as e:
			logger.error(f"Unable to decode JSON from script output. Error: {e}")
			return {'status': JobStatus.JSON_DECODE_ERROR, 'error': str(e)}
		except Exception as e:
			logger.error(f"Script Error: {e}")
			return {'status': JobStatus.SCRIPT_ERROR, 'error': str(e)}


class ModularWorkflowStrategy(JobWorkflowStrategy):
	"""5-phase pipeline: preparation, [preflight], pre-extraction, simulation, post-extraction.

	Uses a :class:`JobStatusManager` internally to track the job through
	each phase.  If any phase fails the pipeline stops and returns the
	terminal status immediately.

	Attributes
	----------
	preparation_strategy : PreparationStrategy
		Strategy that produces the INP file.
	preflight_mode : str or None
		``'syntaxcheck'``, ``'datacheck'``, or ``None`` (IMP-04).
	pre_extraction_strategies : list[ExtractionStrategy]
		Strategies run before the solver (e.g. property extraction from INP).
	post_extraction_strategies : list[ExtractionStrategy]
		Strategies run after the solver (e.g. result extraction from ODB).
	"""

	def __init__(
		self,
		preparation_strategy: PreparationStrategy,
		pre_extraction_strategies: List[ExtractionStrategy],
		post_extraction_strategies: List[ExtractionStrategy],
		preflight_mode: str | None = None,
		preflight_only: bool = False,
	):
		self.preparation_strategy = preparation_strategy
		self.preflight_mode = preflight_mode
		self.pre_extraction_strategies = pre_extraction_strategies
		self.post_extraction_strategies = post_extraction_strategies
		self.preflight_only = preflight_only

	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Run the modular workflow (5 phases with optional preflight).

		Returns a dict with at least a ``'status'`` key plus any results
		from pre- and post-extraction hooks.  Failing early means later
		phases are skipped.
		"""
		logger.info("Workflow Strategy [ModularWorkflow]: Starting Modular Workflow...")
		status_manager = JobStatusManager()
		all_results: dict = {}

		# 1. Preparation
		if not self.preparation_strategy.prepare(ctx, runner, logger):
			status_manager.record_preparation(success=False)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		status_manager.record_preparation(success=True)

		# 2. Preflight (IMP-04: inserted before pre-extraction for fail-fast)
		if self.preflight_mode:
			logger.info(f"Preflight [{self.preflight_mode}]: checking INP...")
			passed, pf_errors = runner.run_preflight(self.preflight_mode)
			if not passed:
				status_manager.record_preflight(
					success=False,
					error=pf_errors[0] if pf_errors else f"Preflight [{self.preflight_mode}] failed",
				)
				all_results['status'] = status_manager.get_final_status()
				return all_results
			status_manager.record_preflight(success=True)
			logger.info(f"Preflight [{self.preflight_mode}]: passed")

		# IMP-04: preflight_only mode — stop after preflight, skip solver & extraction
		if self.preflight_only:
			all_results['status'] = status_manager.get_final_status()
			return all_results

		# 3. Pre-extraction
		for strategy in self.pre_extraction_strategies:
			pre_ext_results = strategy.extract(ctx, runner, logger)
			status_manager.record_extraction(pre_ext_results)
			all_results.update(pre_ext_results)

		# 4. Simulation (IMP-02: diagnostics-backed verdict)
		solver_result = runner.run_solver()
		# Attach diagnostics on failure and on the rc≠0+COMPLETED edge case
		if solver_result.diagnostics is not None:
			if not solver_result.success or solver_result.error:
				from dataclasses import asdict
				all_results['diagnostics'] = asdict(solver_result.diagnostics)
		if not solver_result.success:
			status_manager.record_simulation(success=False, error=solver_result.error)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		status_manager.record_simulation(success=True)

		# 5. Post-extraction
		for strategy in self.post_extraction_strategies:
			post_ext_results = strategy.extract(ctx, runner, logger)
			status_manager.record_extraction(post_ext_results)
			all_results.update(post_ext_results)

		all_results['status'] = status_manager.get_final_status()
		return all_results