import os
import logging
import json
import uuid
import functools
import sys
import shutil
import re

import subprocess
from multiprocessing import Pool

from pprint import pprint
import numpy as np
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from .strategies import JobWorkflowStrategy
from .strategies import (ModularWorkflowStrategy, MonolithicWorkflowStrategy, 
						InpModifyStrategy, ModelGenerationStrategy,
						OdbExtractionStrategy, ModelPropertiesExtractionStrategy)
from .utils.helpers import check_abqpy_installed
from .status import JobStatus



# TODO: 
# [ ] 全部用str在batch_data里面传递不合理，至少要把Strategy独立出来
# [ ] 异步的处理计算方式

class AbaqusCalculation:
	"""
	上下文类，持有并调用一个总的工作流策略来完成任务。
	
	Methods
	-------
		- execute(): 执行整个工作流, 返回结果字典
		- run_simulation(cpus): 运行Abaqus仿真

		- _robust_json_extractor(text_output): 从文本输出中提取JSON数据
		- _run_single_hook(script_path, tasks, common_args): 运行单个提取钩子脚本
	
	Properties
	----------

	
	"""
	def __init__(
		self,
		job_name         : str,
		output_dir       : str,
		workflow_strategy: JobWorkflowStrategy,
		cpus_per_job     : int,
		abaqus_exe       : str = 'abaqus'
	):
		self.job_name = job_name
		self.output_dir = output_dir
		self.workflow_strategy = workflow_strategy
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		
		self.inp_path = os.path.join(self.output_dir, f"{self.job_name}.inp")
		self.odb_path = os.path.join(self.output_dir, f"{self.job_name}.odb")
		self.log_path = os.path.join(self.output_dir, f"{self.job_name}.log")
		os.makedirs(self.output_dir, exist_ok=True)
		self.logger = None

	def execute(self):
		if self.logger is None:
			self.logger = self._setup_logging()
		
		self.logger.info(f"======== [AbaqusCalculation] Start Workflow: {self.job_name} ========")
		results = self.workflow_strategy.execute(self)
		self.logger.info(f"======== [AbaqusCalculation] Workflow Finished: {self.job_name} ========")
		return results

	def _setup_logging(self) -> logging.Logger:
		logger = logging.getLogger(f"AbaqusCalculation_{self.job_name}")
		
		if logger.hasHandlers():
			logger.handlers.clear()

		logger.setLevel(logging.INFO)

		# 日志格式
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

		# 输出文件
		file_handler = logging.FileHandler(self.log_path, encoding='utf-8')
		file_handler.setLevel(logging.DEBUG)
		file_handler.setFormatter(formatter)
		logger.addHandler(file_handler)

		return logger

	def run_simulation(self, cpus: int) -> bool:
		"""
		Use 'abaqus job=..., input=inp_file, cpus=num_cpu' to run simulations.

		Parameters
		----------
		cpus: `int`
			Number of CPUs to use for the simulation.

		"""
		self.logger.info(f"======== [AbaqusCalculation] Executor [CLI]: run 'abaqus job={self.job_name}' ========")
		command = [self.abaqus_exe, 'job=' + self.job_name, 'input=' + self.inp_path, 'cpus=' + str(cpus), 'interactive']
		try:
			subprocess.run(command, cwd=self.output_dir, check=True, capture_output=True, text=True)
			self.logger.info(f"======== [AbaqusCalculation] Job '{self.job_name}' successfully executed. ========")
			return True
		except subprocess.CalledProcessError as e:
			self.logger.error(f"======== [AbaqusCalculation] Job '{self.job_name}' failed. STDERR:\n{e.stderr} ========")
			return False

	def _robust_json_extractor(self, text_output: str) -> dict:
		json_start_index = text_output.find('{')
		if json_start_index == -1:
			raise ValueError("Unable to find '{'.")

		json_candidate_string = text_output[json_start_index:]
		
		try:
			return json.loads(json_candidate_string)
		except json.JSONDecodeError as e:
			# 如果失败（通常是因为尾部有无关数据），利用异常信息来切割出有效部分
			try:
				valid_json_part = json_candidate_string[:e.pos]
				return json.loads(valid_json_part)
			except json.JSONDecodeError:
				# 如果这样仍然失败，说明JSON本身格式有问题
				raise ValueError(f"无法解析截取出的JSON部分: '{valid_json_part[:100]}...'")

	def _run_single_hook(self, script_path: str, tasks: list, common_args: dict) -> dict:
		"""
		Run a **single** extraction hook script with **multiple** tasks.

		Parameters
		----------
		script_path: `str`
			Path to the extraction script.
		tasks: `list`
			List of task dictionaries to be passed to the script.
		common_args: `dict`
			Common arguments to be passed to the script (e.g., --odb_path, --inp_path).

		Returns
		-------
		dict
			A dictionary mapping each task's 'result_name' to its extracted value.
		"""
		if not tasks:
			return {}
		
		# 缺点：需要创建一个临时json文件来传递任务列表
		temp_json_path = os.path.join(self.output_dir, f"tasks_{uuid.uuid4().hex}.json")
		
		try:
			with open(temp_json_path, 'w', encoding='utf-8') as f:
				json.dump(tasks, f, indent=4)

			# 移除对于abqpy的依赖
			has_abqpy = check_abqpy_installed()
			if has_abqpy:
				command = ['python', script_path]
			else:
				if common_args.get('--inp_path'):
					command = [self.abaqus_exe, 'cae noGUI=', script_path]
				else:
					command = [self.abaqus_exe, 'python', script_path]

			for key, value in common_args.items():
				command.extend([key, value])
			
			command.extend(['--tasks_json', temp_json_path])
			
			process = subprocess.run(command, check=True, capture_output=True, text=True)
			
			out = self._robust_json_extractor(process.stdout)

			return out

		except Exception as e:
			self.logger.error(f"Run extraction '{script_path}' failed: {e}")
			stderr_output = getattr(e, 'stderr', 'N/A')
			stdout_output = getattr(e, 'stdout', 'N/A')
			self.logger.error(f"Captured stderr:\n{stderr_output}")
			self.logger.error(f"Captured stdout:\n{stdout_output}")
			return {task['result_name']: None for task in tasks}
		finally:
			if os.path.exists(temp_json_path):
				os.remove(temp_json_path)		

	# Obselete
	def _run_extraction_hook_engine(self, hooks: list, common_args: dict) -> dict:
		"""
		obselete method, kept for backward compatibility.
		通用的钩子执行引擎，可以运行任何提取脚本。
		
		Args:
			hooks (`list`): Hooks configuration
			common_args (`dict`): Common args passed to each hook (--odb_path or --inp_path)
		"""
		results = {}
		for hook in hooks:
			hook = hook.copy()
			result_name = hook.pop('result_name')
			script_path = hook.pop('script_path')
			
			# 移除对于abqpy的依赖
			has_abqpy = check_abqpy_installed()
			if has_abqpy:
				command = ['python', script_path]
			else:
				if common_args.get('--inp_path'):
					command = [self.abaqus_exe, 'cae noGUI=', script_path]
				else:
					command = [self.abaqus_exe, 'python', script_path]
			

			# 添加通用参数
			for key, value in common_args.items():
				command.extend([key, value])
			# 添加钩子特定参数
			for key, value in hook.items():
				command.extend([f'--{key}', str(value)])
			
			try:
				process = subprocess.run(command, check=True, capture_output=True, text=True)
				output_str = process.stdout.strip()
				output_lines = output_str.splitlines()
				if not output_lines:
					self.logger.error("The script produced no output on stdout.")

				# Find the numeric result line from the end of the output
				numeric_result_line = None
				for line in reversed(output_lines):
					try:
						float(line.strip())
						numeric_result_line = line.strip()
						break
					except ValueError:
						continue

				if numeric_result_line is None:
					self.logger.error("Could not find a valid numeric value in the script's output.")

				# 5. Convert the found numeric line to a float.
				results[result_name] = float(numeric_result_line)
				self.logger.info(f"Extract '{result_name}' successfully with value: {numeric_result_line}")

			except Exception as e:
				self.logger.error(f"Extract '{result_name}' (script: {script_path}) failed: {e}")
				self.logger.error(f"Captured full stdout:\n---\n{process.stdout}\n---")
				self.logger.error(f"Captured full stderr:\n---\n{process.stderr}\n---")
				results[result_name] = None
		return results
	

class BatchAbaqusProcessor:
	"""
	Run multiple Abaqus calculations in parallel based on a batch configuration.


	Method
	------
		- run_batch(num_parallel_jobs): Run the batch of calculations in parallel.
	
	"""
	def __init__(
		self,
		batch_data:         list[dict],
		base_output_dir:    str,
		cpus_per_job:       int,
		abaqus_exe:         str = 'abaqus',
		duplicate_mode:     str = 'interactive',
	):
		"""
		Parameters
		----------
		batch_data: `list[dict]`
			List of job configurations. Each dict should contain:
				- 'job_name': str, name of the job
				- 'workflow': str, either 'modular' or 'monolithic'
				- 'type': str, type of preparation ('inp_based' or 'model_generation')
				- 'base_inp_path': str, path to base .inp file (for inp_based)
				- 'model_script_path': str, path to model generation script (for model_generation)
				- 'params': dict, parameters for preparation
				- 'pre_extraction': list[dict], pre-extraction hooks
				- 'post_extraction': list[dict], post-extraction hooks
		base_output_dir: `str`
			Base directory where all job output directories will be created.
		cpus_per_job: `int`
			Number of CPUs to allocate for each job.
		abaqus_exe: `str`, default = 'abaqus'
			Path to the Abaqus executable.
		duplicate_mode: `str`, default = 'interactive'
			How to handle existing job directories. Options:
				- 'fail': abort the batch if any job directory exists.
				- 'skip': skip jobs with existing directories.
				- 'overwrite': overwrite existing job directories.
				- 'interactive': prompt the user for each conflict.
		
		"""
		self.batch_data = batch_data
		self.base_output_dir = base_output_dir
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe

		self.duplicate_mode = duplicate_mode.lower()
		self._overwrite_all = None  # None: 未决定, True: 全部覆盖, False: 全部跳过

		self._log_path = os.path.join(self.base_output_dir, 'batch_processor.log')

		self.logger = self._setup_logging()

		self.calculations = self._initialize_calculations()

	def _setup_logging(self) -> logging.Logger:
		logger = logging.getLogger('BatchAbaqusProcessor')
		
		if logger.hasHandlers():
			logger.handlers.clear()

		logger.setLevel(logging.INFO)

		# 日志格式
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

		file_handler = logging.FileHandler(self._log_path, mode='a', encoding='utf-8')

		file_handler.setFormatter(formatter)
		logger.addHandler(file_handler)

		logger.info("======== Batch Processor Start ========")
		logger.info(f"Duplicate mode: {self.duplicate_mode}")

		return logger


	def _get_name_pattern(self, job_name: str) -> tuple[str, bool]:
		"""
		Retrieve a pattern for the job name.

		Examples:
			>>> _get_name_pattern('job_123')
			('job_*', True)
			>>> _get_name_pattern('job')
			('job', False)
			>>> _get_name_pattern('job_123_v2')
			('job_*', True)
			>>> _get_name_pattern('job_v2')
			('job_*', True)
			>>> _get_name_pattern('job_v2_123')
			('job_v2_*', True)
		"""
		match = re.match(r'^(.*?)_?(\d+)$', job_name)
		if match:
			return f"{match.group(1)}_*", True
		return job_name, False

	def _find_available_job_name(self, original_name: str, base_dir: str) -> str:
		version = 2
		while True:
			new_name = f"{original_name}_v{version}"
			new_path = os.path.join(base_dir, new_name)
			if not os.path.isdir(new_path):
				return new_name
			version += 1
		

	def _initialize_calculations(self):
		# ------------- 1. 检查输出冲突 ------------------------
		conflicts = [cfg for cfg in self.batch_data if os.path.isdir(os.path.join(self.base_output_dir, cfg['job_name']))]
		decisions = {} # 存储对每个冲突任务的决策: {'job_name': 'skip' | 'overwrite' | 'new_name'}
		pattern_decisions = {} # 存储对特定命名模式的决策 {'pattern_name': 'skip' | 'overwrite' | 'rename'}

		if conflicts:
			conflict_names = [c['job_name'] for c in conflicts]
			self.logger.warning(f"Job output directory exists - Num:{len(conflicts)}, Names: {', '.join(conflict_names)}")

			if self.duplicate_mode == 'fail':
				raise FileExistsError(f"Mode[Fail] - Batch processing aborted since jobs exist: {', '.join(conflict_names)}")
			if self.duplicate_mode == 'skip':
				self.logger.info(f"Mode[Skip] - Skipping existing jobs: {', '.join(conflict_names)}")
				for name in conflict_names: 
					decisions[name] = 'skip'
			if self.duplicate_mode == 'overwrite':
				self.logger.info(f"Mode[Overwrite] - Overwriting existing jobs: {', '.join(conflict_names)}")
				for name in conflict_names:
					decisions[name] = 'overwrite'
			if self.duplicate_mode == 'interactive':
				overwrite_all, skip_all = False, False
				for job_config in conflicts:
					job_name = job_config['job_name']
					if overwrite_all:
						decisions[job_name] = 'overwrite'
						continue
					if skip_all:
						decisions[job_name] = 'skip'
						continue

					# TODO: _get_name_pattern()意义在于？
					pattern, has_pattern = self._get_name_pattern(job_name)
					if has_pattern and pattern in pattern_decisions:
						decision = pattern_decisions[pattern]
						if decision == 'skip' or decision == 'overwrite':
							decisions[job_name] = decision
						else: # decision is 'rename'
							decisions[job_name] = self._find_available_job_name(job_name, self.base_output_dir)
						continue

					while True:
						prompt = (f"\n Job '{job_name}' already exists. Choose one option from below:\n"
									f"  [o]verwrite:       overwrite this job\n"
									f"  [s]kip:            skip this job\n"
									f"  [r]ename:          rename and run (e.g., '{job_name}_v2')\n"
									f"  [O]verwrite All:   overwrite all the following jobs\n"
									f"  [S]kip All:        skip all the following jobs\n"
									f"  [P]attern:         apply the decisions to similar jobs with '{job_name}'\n"
									f"  [A]bort:           abort the batch processing\n"
									f"  >>> ")
						response = input(prompt).strip()
						if response == 'o':
							decisions[job_name] = 'overwrite'
							break
						elif response == 's':
							decisions[job_name] = 'skip'
							break
						elif response == 'r':
							new_name = self._find_available_job_name(job_name, self.base_output_dir)
							decisions[job_name] = new_name
							break
						elif response == 'O':
							overwrite_all = True
							decisions[job_name] = 'overwrite'
							break
						elif response == 'S':
							skip_all = True
							decisions[job_name] = 'skip'
							break
						elif response == 'P':
							while True:
								sub_prompt = (f"  -> Apply decisions to similar jobs with '{pattern}':\n"
					  							f"  [o]verwrite all \n"
												f"  [s]kip all? \n"
												f"  [r]ename all (e.g., '{pattern.replace('*', '')}_v2')\n"
												f"  >>> ")
								sub_res = input(sub_prompt).lower().strip()
								if sub_res in ['o', 'overwrite']:
									pattern_decisions[pattern] = 'overwrite'
									decisions[job_name] = 'overwrite'
									break
								elif sub_res in ['s', 'skip']:
									pattern_decisions[pattern] = 'skip'
									decisions[job_name] = 'skip'
									break
								elif sub_res in ['r', 'rename']:
									pattern_decisions[pattern] = 'rename'
									decisions[job_name] = self._find_available_job_name(job_name, self.base_output_dir)
									break									
								else: 
									print("  Invalid input, please try again.")
							break
						elif response == 'a':
							raise ValueError("User aborted the batch processing.")
						else: 
							print("Invalid input, please try again.")

		# ------------- 2. 初始化计算实例 ------------------------
		calcs = []

		for job_config in self.batch_data:
			original_job_name = job_config['job_name']

			decision = decisions.get(original_job_name)

			if decision == 'skip':
				self.logger.info(f"  - Skipping job: {original_job_name}")
				continue		
			elif decision == 'overwrite':
				self.logger.info(f"  - Overwriting job: {original_job_name} (removing old directory)")
				shutil.rmtree(os.path.join(self.base_output_dir, original_job_name))			
			elif decision is not None: # 'new_name'
				self.logger.info(f"  - Renaming job: {original_job_name} -> {decision}")
				job_config['job_name'] = decision


			workflow_type = job_config.get('workflow', 'modular')

			workflow_strategy: JobWorkflowStrategy
			if workflow_type == 'modular':
				prep_type = job_config.get('type', 'inp_based')

				# Preparation strategies
				if prep_type == 'inp_based':
					prep_strategy = InpModifyStrategy(job_config['base_inp_path'], job_config['params'])
				elif prep_type == 'model_generation':
					prep_strategy = ModelGenerationStrategy(job_config['model_script_path'], job_config['params'])
				else:
					raise ValueError(f"Unsupported preparation type: {prep_type}")
				
				# Pre & Post extraction strategies
				pre_extraction_hooks = job_config.get('pre_extraction', [])
				post_extraction_hooks = job_config.get('post_extraction', [])

				pre_ext_strategies = [ModelPropertiesExtractionStrategy(pre_extraction_hooks)] if pre_extraction_hooks else []
				post_ext_strategies = [OdbExtractionStrategy(post_extraction_hooks)] if post_extraction_hooks else []
				
				# Workflow strategies
				workflow_strategy = ModularWorkflowStrategy(prep_strategy, pre_ext_strategies, post_ext_strategies)
			elif workflow_type == 'monolithic':
				workflow_strategy = MonolithicWorkflowStrategy(job_config['script_path'], job_config['params'])
			else:
				raise ValueError(f"Unsupported workflow: {workflow_type}")

			calc = AbaqusCalculation(
				job_name=job_config['job_name'],
				output_dir=os.path.join(self.base_output_dir, job_config['job_name']),
				workflow_strategy=workflow_strategy,
				cpus_per_job=self.cpus_per_job,
				abaqus_exe=self.abaqus_exe
			)
			calcs.append(calc)

		self.logger.info("======== Batch Processor Finished ========")
		return calcs

	def run_batch(self, num_parallel_jobs: int, output_type: str = 'list'):
		"""
		Run all calculations in parallel using multiprocessing.
		
		Parameters
		----------
		num_parallel_jobs: `int`
			Number of parallel jobs to run.
		output_type: `str`
			Type of output, either 'list' or 'dict'.

		Returns
		-------
			list[`dict`] or dict[`str`, `dict`]: results of all calculations.

		Example
		-------
		>>> processor.run_batch(num_parallel_jobs=4, output_type='list')
		[
			{
				'total_mass': 0.000320662622552476,
				'max_stress_mises': 4525.26025390625,
				'max_displacement': 4.189039707183838,
				'status': 'COMPLETED',
				'job_name': 'test_inp_based_job'
			},
			...
		]

		>>> processor.run_batch(num_parallel_jobs=4, output_type='dict')
		{
			'test_inp_based_job': {
				'total_mass': 0.000320662622552476,
				'max_stress_mises': 4525.26025390625,
				'max_displacement': 4.189039707183838,
				'status': 'COMPLETED'}
			...
		}

		"""
		total_tasks = len(self.calculations)

		progress_columns = [
			SpinnerColumn(),
			TextColumn("[progress.description]{task.description}", justify="right"),
			BarColumn(),
			TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
			TextColumn("({task.completed} of {task.total})"),
			TimeElapsedColumn(),
		]
		
		with Progress(*progress_columns) as progress:
			main_task = progress.add_task("[bold blue]Running calculations...", total=total_tasks)

			success_callback = functools.partial(_log_success_callback, progress, main_task)
			error_callback = functools.partial(_log_error_callback, progress, main_task)

			pool = Pool(processes=num_parallel_jobs)
			async_results = []
			
			for calc in self.calculations:
				res = pool.apply_async(
					_run_workflow_worker_async, 
					args=(calc,), 
					callback=success_callback, 
					error_callback=error_callback
				)
				async_results.append(res)
				
			pool.close()
			pool.join()
		
		if output_type == 'dict':
			final_results = {}
			for res in async_results:
				job_name, job_result = res.get()
				if job_result and isinstance(job_result, dict):
					final_results[job_name] = job_result
		elif output_type == 'list':
			final_results = []
			for res in async_results:	# res: `multiprocessing.pool.AsyncResult`, use .get() to retrieve the result	
				job_name, job_result = res.get()
				if job_result and isinstance(job_result, dict):
					job_result['job_name'] = job_name
					final_results.append(job_result)
		else:
			raise ValueError(f"Unsupported output type: {output_type}. Use 'list' or 'dict'.")

		
		return final_results

# -------------------------------------------------------------
# Helper functions for multiprocessing
def _run_workflow_worker_async(calc_instance: AbaqusCalculation):
	results = calc_instance.execute()
	return (calc_instance.job_name, results)

# Async callbacks
def _log_success_callback(progress, task_id, result_tuple):
	job_name, results = result_tuple
	status = results.get('status', JobStatus.UNKNOWN)
	progress.update(task_id, advance=1, description=f"{job_name} Finished (Status: {status})")

def _log_error_callback(progress, task_id, exception):
	# TODO: 不知道什么情况会error
	progress.update(task_id, advance=1, description=f"❌ 失败: 一个任务遇到错误 ({type(exception).__name__})")

# -------------------------------------------------------------
# Utils for batch job generation and result extraction
def generate_from_array(samples_array, param_names, base_config) -> list[dict]:
	"""
	Generate batch job configurations from a numerical array (numpy or torch).

	Args:
		samples_array (`np.ndarray` or `torch.Tensor`): size (n_samples, n_dim)
		param_names (`list[str]`): A list of strings of length n_dim specifying the parameter names corresponding to each column of the array.
		base_config (`dict`): The base configuration shared by all tasks.

	Returns:
		`list[dict]`: list of generated batch_jobs_data.
	"""

	if hasattr(samples_array, 'numpy'):
		samples_array = samples_array.numpy()

	n_samples, n_dim = samples_array.shape
	if n_dim != len(param_names):
		raise ValueError(f"Dim of samples_array ({n_dim}) is not consistent with param_names ({len(param_names)})")

	batch_jobs_data = []
	for i in range(n_samples):
		sample_values = samples_array[i, :]
		job_params = dict(zip(param_names, sample_values))
		job_config = base_config.copy()
		job_name = job_config.pop('job_name', 'job_array_run_')
		job_config['params'] = job_params
		job_config['job_name'] = f"{job_name}_{i+1:04d}" # e.g., job_array_run_0001
		batch_jobs_data.append(job_config)
		
	return batch_jobs_data

def degenerate_from_array(results, output_names, default_value=np.nan) -> np.ndarray:
	"""
	Depack results from a batch job into a 2D numpy array.

	Args:
		results (`list[dict]`): List of results dictionaries from the batch job.
		output_names (`list[str]`): List of output names to extract from each result.
		default_value (optional, default=np.nan): Value to use if an output is missing in a result.
		
	Returns:
		`np.ndarray`: A 2D numpy array where each row corresponds to a job and each column corresponds to an output name.
	"""
	if results and 'job_name' in results[0]:
		# 按 job_name 排序以确保一致的顺序
		results = sorted(results, key=lambda x: x.get('job_name', ''))
	
	output_array = []
	for job_result in results:
		output_row = [job_result.get(name, default_value) for name in output_names]
		output_array.append(output_row)	
	
	return np.array(output_array)
