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
	`PreparationStrategy` prepares the job with **generating an INP file**.
	
	Methods Subclass should implement:
	- prepare(context: `AbaqusCalculation`): generate one INP file in `context.output_dir`
	"""

	@abstractmethod
	def prepare(self, context: AbaqusCalculation) -> bool:
		pass

class InpModifyStrategy(PreparationStrategy):
	"""
	Prepare the job by modify a current INP file.

	Properties in the INP file must be defined as placeholders like {{property_name}}.
	
	`Example INP file`:

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
	"""
	`ExtractionStrategy` defines how to extract results from the simulation.

	Methods Subclass should implement:
	- extract(context: `AbaqusCalculation`): execute the extraction logic and return a dictionary of results.
	
	"""
	@abstractmethod
	def extract(self, context: AbaqusCalculation) -> dict:
		pass

class OdbExtractionStrategy(ExtractionStrategy):
	# TODO: Docstring
	"""
	Extract results from ODB files using user-defined scripts.
	
	.. rubric:: 用户自定义“ODB提取”钩子脚本编写规范

	1.  **执行环境**: 必须能由 `abaqus python` 执行并可以 `import odbAccess`。
	2.  **命令行接口**:
			- 必须使用 `argparse` 解析参数。
			- 必须接收由框架传入的 `--odb_path` 参数。
			- 可以定义任何自定义参数来定位数据（如 `--step`, `--node_label` 等）可以在post_extraction.hooks字段中配置使用。
	3.  **核心任务**:
			- 打开 `odb_path` 指定的ODB文件，并提取所需数据。
	4.  **标准输出 (stdout)**:
			- **必须**向标准输出打印一个**单一的、可以被Python的`float()`函数转换的数值**。不要打印任何其他信息。
	5.  **错误处理**:
			- 必须遵循通用规范中的错误处理流程。

	**Example Script**:
	.. code-block:: python

		import argparse, sys, odbAccess

		if __name__ == "__main__":
			parser = argparse.ArgumentParser()
			parser.add_argument('--odb_path', required=True)
			# ... add your custom args like --node_label ...
			args = parser.parse_args()
			extract_with_your_script(args)

		def extract_with_your_script(args):
			try:
				odb = odbAccess.openOdb(args.odb_path)
				# ... odb access and data extraction logic ...
				result = 123.45
				print(result)

			except Exception as e:
				print(e, file=sys.stderr)
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
	# TODO: Docstring
	"""
	通过ASI从MDB获取模型属性的策略 (仿真前)。

	.. rubric:: 用户自定义“模型属性提取”钩子脚本编写规范

	1.  **执行环境**: 必须能由 `python` 执行并可以 `from abaqus import mdb`。
	2.  **命令行接口**:
		- 必须使用 `argparse` 解析参数。
		- 必须接收由框架传入的 `--inp_path` 参数。
		- 可以定义 `--property` 等自定义参数来指定要提取的属性。
	3.  **核心任务**:
		- 使用 `mdb.ModelFromInputFile()` 将INP文件加载到内存中。
		- 通过ASI查询模型属性，如 `assembly.getMassProperties()`。
	4.  **标准输出 (stdout)**:
		- **必须**向标准输出打印一个**单一的、可以被Python的`float()`函数转换的数值**。
	5.  **错误处理**:
		- 必须遵循通用规范。

	**Example Script**:
	.. code-block:: python

		import argparse, sys
		from abaqus import mdb

		if __name__ == "__main__":
			parser = argparse.ArgumentParser()
			parser.add_argument('--odb_path', required=True)
			# ... add your custom args like --node_label ...
			args = parser.parse_args()
			extract_with_your_script(args)

		def extract_with_your_script(args):
			try:
				mdb.ModelFromInputFile(name='test', inputFileName=args.inp_path)
				# ... mdb access and data extraction logic ...
				result = 123.45

				# Bypass the default stdout to avoid Abaqus' print redirection
				sys.__stdout__.write(f"{result}\n")

			except Exception as e:
				print(e, file=sys.stderr)
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
	"""最高层级的策略接口，定义了如何执行一个完整的作业。"""
	@abstractmethod
	def execute(self, context: AbaqusCalculation) -> dict:
		"""执行完整工作流并返回结果字典。"""
		pass

class MonolithicWorkflowStrategy(JobWorkflowStrategy):
	"""
	MonolithicWorkflowStrategy suites for simple tasks where all operations can be handled in a single script.

	Operations should include:
	- Create part
	- Create Materials
	- Create Section
	- Create Assembly
	- Create Step
	- Create Load
	- Create Mesh
	- Run Abaqus Job
	- Extract results

	Refer to [Cantilever Example](https://hailin.wang/abqpy/zh_CN/2025/examples/Abaqus/cantilever.html#sphx-glr-examples-abaqus-cantilever-py) for more details.

	
	.. rubric:: 用户自定义“一体化”脚本编写规范

	1.  **执行环境**: 必须能由 `abaqus python` 执行。
	2.  **命令行接口**:
		- 必须使用 `argparse` 解析参数。
		- 必须接收由框架传入的 `--job_name` 参数，并用它来命名Abaqus Job (`mdb.Job(name=...)`)，以确保输出文件（如.odb）命名一致。
		- 可以定义并接收任何自定义参数（如--length, --height等）。
	3.  **标准输出 (stdout)**:
		- 必须是脚本向框架返回数据的**唯一**通道。
		- **必须**在成功执行后，打印一个**合法的、单一的JSON字符串**。此JSON应包含所有结果。
		- 推荐JSON中包含一个 'status': 'COMPLETED' 键值对。
	4.  **错误处理**:
		- 整个主逻辑必须被包裹在 `try...except` 块中。
		- 发生错误时，必须将错误信息打印到标准错误流 `sys.stderr`。
		- 发生错误时，必须以非零状态码退出 `sys.exit(1)`。

	**脚本骨架示例**:
	.. code-block:: python

		import argparse, json, sys, abaqus
		
		parser = argparse.ArgumentParser()
		parser.add_argument('--job_name', required=True)
		# --- 添加你自己的参数 ---
		parser.add_argument('--my_param', type=float, required=True)
		args = parser.parse_args()
		
		try:
			# 1. Abaqus 建模...
			# 2. 创建并运行 Job, 必须使用 args.job_name
			mdb.Job(name=args.job_name, ...)
			mdb.jobs[args.job_name].submit()
			mdb.jobs[args.job_name].waitForCompletion()
			# 3. 打开 ODB 并后处理...
			results = {'status': 'COMPLETED', 'my_result': 123.45}
			# 4. 打印 JSON 结果
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