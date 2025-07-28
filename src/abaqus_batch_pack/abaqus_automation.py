import os
import logging
import subprocess
from multiprocessing import Pool
import numpy as np

from .strategies import JobWorkflowStrategy
from .strategies import (ModularWorkflowStrategy, MonolithicWorkflowStrategy, 
						InpModifyStrategy, ModelGenerationStrategy,
						OdbExtractionStrategy, ModelPropertiesExtractionStrategy)


class AbaqusCalculation:
	"""上下文类，持有并调用一个总的工作流策略来完成任务。"""
	def __init__(self, job_name, output_dir, workflow_strategy: JobWorkflowStrategy, cpus_per_job: int, abaqus_exe='abaqus'):
		self.job_name = job_name
		self.output_dir = output_dir
		self.workflow_strategy = workflow_strategy
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		
		self.inp_path = os.path.join(self.output_dir, f"{self.job_name}.inp")
		self.odb_path = os.path.join(self.output_dir, f"{self.job_name}.odb")
		self.log_path = os.path.join(self.output_dir, f"{self.job_name}.log")
		os.makedirs(self.output_dir, exist_ok=True)
		# self.logger = self._setup_logging()
		self.logger = None  # 延迟初始化日志记录器

	def execute(self):
		"""执行由其工作流策略定义的完整任务。"""
		if self.logger is None:
			self.logger = self._setup_logging()
		
		self.logger.info(f"======== Start Workflow: {self.job_name} ========")
		results = self.workflow_strategy.execute(self)
		self.logger.info(f"======== Workflow Finished: {self.job_name} ========")
		return results

	def _setup_logging(self):
		logger = logging.getLogger(self.job_name)
		logger.setLevel(logging.INFO)
		if logger.hasHandlers():
			logger.handlers.clear()
		handler = logging.FileHandler(self.log_path)
		formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
		handler.setFormatter(formatter)
		logger.addHandler(handler)
		return logger

	def run_simulation(self, cpus: int) -> bool:
		"""
		所有模块化工作流统一使用的运行器：'abaqus job=...' 命令。
		"""
		self.logger.info(f"Executor [CLI]: run 'abaqus job={self.job_name}'")
		command = [self.abaqus_exe, 'job=' + self.job_name, 'input=' + self.inp_path, 'cpus=' + str(cpus), 'interactive']
		try:
			subprocess.run(command, cwd=self.output_dir, check=True, capture_output=True, text=True)
			self.logger.info(f"Job '{self.job_name}' successfully executed.")
			return True
		except subprocess.CalledProcessError as e:
			self.logger.error(f"Job '{self.job_name}' failed. STDERR:\n{e.stderr}")
			return False
	
	def _run_extraction_hook_engine(self, hooks: list, common_args: dict) -> dict:
		"""
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
			
			if common_args.get('--inp_path'):
				command = ['python', script_path]
			else:
				command = [self.abaqus_exe, 'python', script_path]

			# command = [self.abaqus_exe, 'python', script_path]

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


# TODO: Adopt new batch_config format, support extracting multiple results from a single script.
# base_job_template = {
# 	'workflow': 'modular',
# 	'type': 'inp_based',
# 	'base_inp_path': './Data/abqpy_test/planar_stress_batch/planar_stress_template.inp',

# 	'pre_extraction': [
# 		{
# 			'script_path': './Data/abqpy_test/planar_stress_batch/get_total_mass.py',
# 			'tasks': [
# 				{'result_name': 'total_mass', },
# 			]
# 		},

# 	],
# 	'post_extraction': [
# 		{
# 			'script_path': './Data/abqpy_test/planar_stress_batch/get_max_stress_mises.py', 
# 			'tasks': [
# 				{'result_name': 'max_stress_mises', },
# 			]
# 		}
# 	]
# }

class BatchAbaqusProcessor:
	"""
	Run multiple Abaqus calculations in parallel based on a batch configuration.	
	"""
	def __init__(self, batch_data, base_output_dir, cpus_per_job, abaqus_exe='abaqus'):
		self.batch_data = batch_data
		self.base_output_dir = base_output_dir
		self.cpus_per_job = cpus_per_job
		self.abaqus_exe = abaqus_exe
		self.calculations = self._initialize_calculations()

	def _initialize_calculations(self):
		calcs = []

		for job_config in self.batch_data:
			workflow_type = job_config.get('workflow', 'modular')

			# Workflow strategies
			workflow_strategy: JobWorkflowStrategy
			if workflow_type == 'modular':
				prep_type = job_config.get('type', 'inp_based')

				# Preparation strategies
				if prep_type == 'inp_based':
					prep_strategy = InpModifyStrategy(job_config['base_inp_path'], job_config['params'])
				else:
					prep_strategy = ModelGenerationStrategy(job_config['model_script_path'], job_config['params'])

				# Pre extraction strategies
				pre_ext_strategies = []
				if 'pre_extraction' in job_config:
					for ext_conf in job_config['pre_extraction']:
						if ext_conf['type'] == 'model_properties':
							pre_ext_strategies.append(ModelPropertiesExtractionStrategy(ext_conf['hooks']))
				
				# Post extraction strategies
				post_ext_strategies = [OdbExtractionStrategy(c['hooks']) for c in job_config.get('post_extraction', []) if c['type'] == 'odb']
				
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
		return calcs

	def run_batch(self, num_parallel_jobs):
		"""
		Run all calculations in parallel using multiprocessing.
		Returns:
			list[`dict`]: A list of results for each calculation.
		"""
		with Pool(processes=num_parallel_jobs) as pool:
			results_from_workers = pool.map(_run_workflow_worker, self.calculations)
	
		result_list = []
		for calc, result in zip(self.calculations, results_from_workers):
			result['job_name'] = calc.job_name
			result_list.append(result)
		return result_list
	
	def run_batch_as_dict(self, num_parallel_jobs):
		"""
		**Obselete method, kept for backward compatibility.**
		
		Run all calculations in parallel using multiprocessing.
		Returns:
			`dict`: A dictionary of results for each calculation, keyed by job name.
		"""
		with Pool(processes=num_parallel_jobs) as pool:
			results_from_workers = pool.map(_run_workflow_worker, self.calculations)
		return {calc.job_name: result for calc, result in zip(self.calculations, results_from_workers)}

def _run_workflow_worker(calc_instance):
	return calc_instance.execute()


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
		job_config['params'] = job_params
		job_config['job_name'] = f"job_array_run_{i+1:04d}" # e.g., job_array_run_0001
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
