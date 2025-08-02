from __future__ import annotations
from abc import ABC, abstractmethod
import os
import subprocess
import json
from typing import List, TYPE_CHECKING
if TYPE_CHECKING:
	from .abaqus_automation import AbaqusCalculation

from .status import JobStatus, JobStatusManager

# ==================================
# 准备策略 (Preparation Strategies)
# ==================================

class PreparationStrategy(ABC):
	"""
	PreparationStrategy prepares the job with **generating an INP file**.
	"""

	@abstractmethod
	def prepare(self, context: AbaqusCalculation) -> bool:
		"""Generate one INP file in `context.output_dir`"""
		pass

class InpModifyStrategy(PreparationStrategy):
	"""
	Prepare the job by modify a current INP file.

	Properties in the INP file must be defined as placeholders like {{property_name}}.

	Attributes:
		base_inp_path (str): The path to the base INP file.
		data_params (dict): A dictionary of parameters to replace in the INP file.
	
	Example INP file:
		.. code-block:: text

			*MATERIAL, NAME=STEEL
			*ELASTIC
			{{youngs_modulus}}, 0.3
			*SOLID SECTION, ELSET=TRUSS, MATERIAL=STEEL
			1.0
			*STEP, NAME=Step-1
			*STATIC
			*BOUNDARY
			1, 1, 3, 0.
			*CLOAD
			2, 1, {{load_magnitude}}
	
	"""
	def __init__(self, base_inp_path, data_params):
		self.base_inp_path = base_inp_path
		self.data_params = data_params
	
	def prepare(self, context: AbaqusCalculation) -> bool:
		context.logger.info(f"Sub strategy [InpModify]: Based on INP file '{self.base_inp_path}'")
		try:
			with open(self.base_inp_path, 'r') as f: 
				content = f.read()
			for key, value in self.data_params.items():
				content = content.replace(f"{{{{{key}}}}}", str(value))
			with open(context.inp_path, 'w') as f: 
				f.write(content)
			context.logger.info(f"Successfully create INP file: {context.inp_path}")
			return True
		except Exception as e:
			context.logger.error(f"Sub strategy [InpModify] failed: {e}")
			return False

class ModelGenerationStrategy(PreparationStrategy):
	"""
	Prepare the job by running a model generation script.
	This script should generate an INP file in the `context.output_dir`.

	Attributes:
		model_script_path (str): The path to the model generation script.
		script_params (dict): A dictionary of parameters to pass to the script.
	
	"""
	def __init__(self, model_script_path, script_params):
		self.model_script_path = model_script_path
		self.script_params = script_params

	def prepare(self, context: AbaqusCalculation) -> bool:
		context.logger.info(f"Sub Strategy [ModelGeneration]: Run script '{self.model_script_path}'")
		command = [context.abaqus_exe, 'python', self.model_script_path]
		for key, value in self.script_params.items():
			command.extend([f'--{key}', str(value)])
		command.extend(['--job_name', context.job_name])
		try:
			subprocess.run(command, check=True, capture_output=True, text=True, cwd=context.output_dir)
			context.logger.info("Successfully generated model.")
			return os.path.exists(context.inp_path)
		except subprocess.CalledProcessError as e:
			context.logger.error(f"Sub Strategy [ModelGeneration] fail. STDERR:\n{e.stderr}")
			return False

# ==================================
# 提取策略 (Extraction Strategies)
# ==================================

class ExtractionStrategy(ABC):
	"""ExtractionStrategy defines how to extract results from the simulation."""
	@abstractmethod
	def extract(self, context: AbaqusCalculation) -> dict:
		"""Execute the extraction logic and return a dictionary of results."""
		pass

class OdbExtractionStrategy(ExtractionStrategy):
	"""
	
	Extract results from ODB files using user-defined scripts.

	Execution Environment:
		- `abaqus python`
		- `python` (if `abaqpy` is installed)
	Command-line Interface:
		- Use `argparse` to parse arguments
		- Required:
			- `--odb_path`: Path to the ODB file
			- `--tasks_json`: Path to a JSON file containing tasks to extract

	Standard Output:
		- Use `sys.__stdout__.write(json.dumps(result))` to redirect output

	Example:
		.. code-block:: python

			import argparse, sys, json
			import odbAccess

			if __name__ == "__main__":
				parser = argparse.ArgumentParser()
				parser.add_argument('--odb_path', type=str, required=True)
				parser.add_argument('--tasks_json', type=str, required=True)

				# ... add your custom args like --node_label ...
				args, unknown = parser.parse_known_args()
				extract_with_your_script(args.odb_path, args.tasks_json, **kwargs)

			def extract_with_your_script(odb_path, tasks_json, **kwargs):
				try:
					# Retrieve tasks
					with open(tasks_json_path, 'r', encoding='utf-8') as f: 
						task_list = json.load(f)

					odb = odbAccess.openOdb(args.odb_path)

					for task in task_list:
						result_name = task['result_name']
						try:
							results[result_name] = 123.45
						except Exception as e:
							results[result_name] = None
							sys.__stderr__.write(f"  - Sub-task '{result_name}' failed: {e}\\n")
					
					odb.close()
					sys.__stdout__.write(json.dumps(results, indent=4) + "\\n")

				except Exception as e:
					sys.__stderr__.write(f"Fatal error in xxxx.py: {e}\\n")
					sys.exit(1)
		
		
	"""
	def __init__(self, hooks):
		# hooks:
		# [
		# 	{
		# 		'script_path': './test/test_file/get_total_mass.py',
		# 		'tasks': [
		# 			{'result_name': 'total_mass'},
		# 		]
		# 	},
		# ],
		self.hooks = hooks

	def extract(self, context: AbaqusCalculation) -> dict:
		context.logger.info("Sub strategy [OdbExtract]: Start extracting from ODB...")

		if not os.path.exists(context.odb_path):
			context.logger.error(f"ODB file does not exist : {context.odb_path}, unable to extract from ODB.")
			all_results = {}
			for hook in self.hooks:
				for task in hook['tasks']:
					all_results[task['result_name']] = None
			return all_results
		
		all_results = {}
		for hook in self.hooks:
			# hook:
			# {
			# 	'result_name': 'max_stress_mises', 
			# 	'script_path': './get_max_stress_mises.py', 
			# }
			script_path = hook['script_path']
			tasks = hook['tasks']
			context.logger.info(f"  -> Run ODB hook script: {script_path} ({len(tasks)} tasks in total)")
			results_from_script = context._run_single_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--odb_path': context.odb_path}
			)
			all_results.update(results_from_script)
		return all_results

class ModelPropertiesExtractionStrategy(ExtractionStrategy):
	"""
	Extract results from INP files using user-defined scripts.

	Execution Environment:
		- `abaqus cae noGui=...`
		- `python` (if `abaqpy` is installed)

	Command-line Interface:
		- Use `argparse` to parse arguments
		- Required:
			- `--inp_path`
			- `--tasks_json`

	Standard Output:
		- Use `sys.__stdout__.write(json.dumps(result))` to redirect output

	Example:
		.. code-block:: python

			import argparse, sys
			from abaqus import mdb

			if __name__ == "__main__":
				parser = argparse.ArgumentParser()
				parser.add_argument('--inp_path', type=str, required=True)
				parser.add_argument('--tasks_json', type=str, required=True)

				# ... add your custom args like --node_label ...
				args, unknown = parser.parse_known_args()
				extract_with_your_script(args.inp_path, args.tasks_json, **kwargs)

			def extract_with_your_script(inp_path, tasks_json, **kwargs):
				try:
					with open(tasks_json_path, 'r', encoding='utf-8') as f: 
						task_list = json.load(f)

					mdb.ModelFromInputFile()

					for task in task_list:
						result_name = task['result_name']
						try:
							results[result_name] = 123.45
						except Exception as e:
							results[result_name] = None
							sys.__stderr__.write(f"  - Sub-task '{result_name}' failed: {e}\\n")
					
					mdb.close()
					sys.__stdout__.write(json.dumps(results, indent=4) + "\\n")

				except Exception as e:
					sys.__stderr__.write(f"Fatal error in xxxx.py: {e}\\n")
					sys.exit(1)
	"""

	def __init__(self, hooks):
		# hooks:
		# [
		# 	{
		# 		'script_path': './test/test_file/get_total_mass.py',
		# 		'tasks': [
		# 			{'result_name': 'total_mass'},
		# 		]
		# 	},
		# ]
		self.hooks = hooks

	def extract(self, context: AbaqusCalculation) -> dict:
		context.logger.info("Sub strategy [ModelPropsExtract]: Start extracting from INP...")
		if not os.path.exists(context.inp_path):
			context.logger.error(f"INP file does not exist: {context.inp_path}, unable to extract model properties.")
			all_results = {}
			for hook in self.hooks:
				for task in hook['tasks']: 
					all_results[task['result_name']] = None
			return all_results
		
		all_results = {}
		for hook in self.hooks:
			# {
			# 	'script_path': './test/test_file/get_total_mass.py',
			# 	'tasks': [
			# 		{'result_name': 'total_mass'},
			# 	]
			# }
			script_path, tasks = hook['script_path'], hook['tasks']
			context.logger.info(f"  -> Run model property hook script: {script_path} ({len(tasks)} jobs in total)")
			results_from_script = context._run_single_hook(script_path, tasks, {'--inp_path': context.inp_path})
			all_results.update(results_from_script)
		return all_results

# ==================================
# 工作流策略 (Workflow Strategies)
# ==================================
class JobWorkflowStrategy(ABC):
	"""
	Defines the interface for job workflow strategies.
	"""
	@abstractmethod
	def execute(self, context: AbaqusCalculation) -> dict:
		"""Execute the complete workflow and return a dictionary of results."""
		pass

class MonolithicWorkflowStrategy(JobWorkflowStrategy):
	"""
	MonolithicWorkflowStrategy suites for simple tasks where all operations can be handled in a single script.

	Operations include:
	- Create part
	- Create materials
	- Create section
	- Create assembly
	- Create step
	- Create load
	- Create mesh
	- Run Abaqus job
	- Extract results

	Refer to the `Cantilever Example <https://hailin.wang/abqpy/zh_CN/2025/examples/Abaqus/cantilever.html#sphx-glr-examples-abaqus-cantilever-py>`_ for more details.

	Execution Environment:
		- Run with `abaqus cae noGUI=` or `python` (if `abaqpy` is installed)

	Command-Line Interface:
		- Must use `argparse` to parse arguments
		- Must accept a `--job_name` argument passed by the framework, and use it as the name of the Abaqus Job (`mdb.Job(name=...)`)
		- May accept any custom arguments (e.g. `--length`, `--height`, etc.)

	Standard Output (stdout):
		- **Must** be the **only** way to return results to the framework
		- On successful execution, print a **single valid JSON string**
		- It is recommended to include a `"status": "COMPLETED"` key in the JSON

	Example:
		.. code-block:: python

			import argparse, json, sys, abaqus

			parser = argparse.ArgumentParser()
			parser.add_argument('--job_name', required=True)
			# --- Add your own arguments ---
			parser.add_argument('--my_param', type=float, required=True)
			args = parser.parse_args()

			try:
				# 1. Abaqus modeling...
				# 2. Create and run job with args.job_name
				mdb.Job(name=args.job_name, ...)
				mdb.jobs[args.job_name].submit()
				mdb.jobs[args.job_name].waitForCompletion()
				# 3. Open ODB and post-process...
				results = {'status': 'COMPLETED', 'my_result': 123.45}
				# 4. Print JSON result
				print(json.dumps(results))
			except Exception as e:
				print(f"Error: {e}", file=sys.stderr)
				sys.exit(1)
	"""

	def __init__(self, script_path, params):
		self.script_path = script_path
		self.params = params

	def execute(
		self,
		context: AbaqusCalculation
	) -> dict:
		"""
		Args:
			context (`AbaqusCalculation`): A AbaqusCalculation instance
		Returns:
			`dict`: Dict including results/errors
		"""
		context.logger.info(f"Workflow [MonolithicWorkflow]: Run Monolithic Script '{self.script_path}'")
		command = [context.abaqus_exe, 'python', self.script_path]
		for key, value in self.params.items():
			command.extend([f'--{key}', str(value)])

		try:
			process = subprocess.run(command, check=True, capture_output=True, text=True, cwd=context.output_dir)
			results = json.loads(process.stdout)
			if 'status' not in results:
				results['status'] = JobStatus.COMPLETED
			context.logger.info("Monolithic script run successfully.")
			return results
		except subprocess.CalledProcessError as e:
			context.logger.error(f"Monolithic script run failed[Caused by `multiprocessing`]. STDERR:\n{e.stderr}")
			return {'status': JobStatus.MONOLITHIC_SCRIPT_FAILED, 'error': e.stderr}
		except json.JSONDecodeError as e:
			context.logger.error(f"Unable to decode JSON from script output[Caused by script output code]. STDOUT:\n{getattr(e, 'doc', '')}")
			return {'status': JobStatus.JSON_DECODE_ERROR, 'error': str(e)}
		except Exception as e:
			context.logger.error(f"Script Error[Caused by Abaqus script]: {e}")
			return {'status': JobStatus.SCRIPT_ERROR, 'error': str(e)}

class ModularWorkflowStrategy(JobWorkflowStrategy):
	"""
	ModularWorkflowStrategy is designed to handle complex workflows by separating preparation, job execution and extraction into distinct strategies.

	Module:

	- Preparation: Prepare the job and export an INP file ready for Abaqus.

	- Execution: Run the Abaqus job using the prepared INP file.
	
	- Extraction: Extract results from INP/ODB file after the job is complete.

	"""

	def __init__(
		self,
		preparation_strategy: PreparationStrategy,
		pre_extraction_strategies: List[ExtractionStrategy],
		post_extraction_strategies: List[ExtractionStrategy]
	):
		self.preparation_strategy = preparation_strategy
		self.pre_extraction_strategies = pre_extraction_strategies
		self.post_extraction_strategies = post_extraction_strategies

	def execute(
		self,
		context: AbaqusCalculation
	) -> dict:
		"""
		Args:
			context (`AbaqusCalculation`): A AbaqusCalculation instance
		Returns:
			`dict`: Dict including results/errors
		"""

		context.logger.info("Workflow Strategy [ModularWorkflow]: Starting Modular Workflow...")
		status_manager = JobStatusManager()
		all_results = {}

		# 1. Preparation
		if not self.preparation_strategy.prepare(context):
			status_manager.record_preparation(success=False)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		else:
			status_manager.record_preparation(success=True)
			
		# 2. Pre-extraction
		for strategy in self.pre_extraction_strategies:
			pre_ext_results = strategy.extract(context)
			status_manager.record_extraction(pre_ext_results)
			all_results.update(pre_ext_results)

		# 3. Run simulation
		run_successful = context.run_simulation(cpus=context.cpus_per_job)
		if not run_successful:
			status_manager.record_simulation(success=False)
			all_results['status'] = status_manager.get_final_status()
			return all_results
		else:
			status_manager.record_simulation(success=True)

		# 4. Post-extraction
		for strategy in self.post_extraction_strategies:
			post_ext_results = strategy.extract(context)
			status_manager.record_extraction(post_ext_results)
			all_results.update(post_ext_results)

		all_results['status'] = status_manager.get_final_status()
		
		return all_results