"""Job workflow strategies — refactored to depend on (JobContext, AbaqusRunner, Logger) only.

Fixes: B5/B6 (execution environment), B7 (JSON via sentinel), B8 (placeholder validation), B9 (error message).
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
from .status import JobStatus, JobStatusManager

# Regex for {{placeholder}} in INP files (B8)
_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


# ======================== Preparation Strategies ========================
class PreparationStrategy(ABC):
	"""Generate an INP file in ctx.output_dir."""

	@abstractmethod
	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool: ...


class InpModifyStrategy(PreparationStrategy):
	"""Replace {{placeholders}} in a base INP file. Validates coverage (B8)."""

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
	"""Run a model-generation script to produce an INP file."""

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


# ======================== Extraction Strategies ========================
class ExtractionStrategy(ABC):
	"""Extract results from simulation outputs."""

	@abstractmethod
	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict: ...


class OdbExtractionStrategy(ExtractionStrategy):
	"""Extract results from ODB via hook scripts. Uses odbAccess environment."""

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
	"""Extract results from INP via hook scripts. Uses CAE kernel (mdb)."""

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
	"""Top-level workflow: preparation → extraction → simulation → extraction."""

	@abstractmethod
	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict: ...


class MonolithicWorkflowStrategy(JobWorkflowStrategy):
	"""Single-script workflow. Execution environment depends on abqpy presence (B5/B6 fix)."""

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
	"""Preparation → pre-extraction → simulation → post-extraction."""

	def __init__(
		self,
		preparation_strategy: PreparationStrategy,
		pre_extraction_strategies: List[ExtractionStrategy],
		post_extraction_strategies: List[ExtractionStrategy],
	):
		self.preparation_strategy = preparation_strategy
		self.pre_extraction_strategies = pre_extraction_strategies
		self.post_extraction_strategies = post_extraction_strategies

	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Workflow Strategy [ModularWorkflow]: Starting Modular Workflow...")
		status_manager = JobStatusManager()
		all_results: dict = {}

		# 1. Preparation
		if not self.preparation_strategy.prepare(ctx, runner, logger):
			status_manager.record_preparation(success=False)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		status_manager.record_preparation(success=True)

		# 2. Pre-extraction
		for strategy in self.pre_extraction_strategies:
			pre_ext_results = strategy.extract(ctx, runner, logger)
			status_manager.record_extraction(pre_ext_results)
			all_results.update(pre_ext_results)

		# 3. Simulation
		run_successful = runner.run_solver()
		if not run_successful:
			status_manager.record_simulation(success=False)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		status_manager.record_simulation(success=True)

		# 4. Post-extraction
		for strategy in self.post_extraction_strategies:
			post_ext_results = strategy.extract(ctx, runner, logger)
			status_manager.record_extraction(post_ext_results)
			all_results.update(post_ext_results)

		all_results['status'] = status_manager.get_final_status()
		return all_results