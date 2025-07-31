import pytest

import numpy as np
import os
import time

from abaqus_batch_pack.abaqus_automation import (
	BatchAbaqusProcessor,
	generate_from_array,
	degenerate_from_array,
)
from abaqus_batch_pack.strategies import (
	InpModifyStrategy,
	ModelGenerationStrategy,
	OdbExtractionStrategy,
	ModelPropertiesExtractionStrategy,
	ModularWorkflowStrategy,
	MonolithicWorkflowStrategy
)

base_job_template = {
	'workflow': 'modular',
	'type': 'inp_based',
	'job_name': 'inp_based_job',
	'base_inp_path': './test/test_file/planar_stress_template.inp',

	'pre_extraction': [
		{
			'script_path': './test/test_file/get_total_mass.py',
			'tasks': [
				{'result_name': 'total_mass'},
			]
		},
	],
	'post_extraction': [
		{
			'script_path': './test/test_file/get_max_stress_mises.py', 
			'tasks': [
				{'result_name': 'max_stress_mises'},
				{'result_name': 'max_displacement'},
			]
		}
	]
}


CPU_PER_JOB = 4
BATCH_SIZE = 1
ABAQUS_CAE = 'C:/Applications/SIMULIA/Commands/abaqus.bat'
OUTPUT_DIR = "C:/SJTU/Projects_Code/24_Abaqus_Pack/test/output"

@pytest.mark.dependency(name="test_generate_from_array")
def test_generate_from_array():
	param_names = ['youngs_modulus', 'load_magnitude']
	param_values = np.array([
		[200000, 2000],
		[210000, 3000],
		[220000, 4000],
		[230000, 5000]
	])

	batch_jobs_data = generate_from_array(param_values, param_names, base_job_template)

	assert len(batch_jobs_data) == 4, "生成的批处理作业数量不正确"

@pytest.mark.dependency(depends=["test_generate_from_array"])
def test_run():
	param_names = ['youngs_modulus', 'load_magnitude']
	param_values = np.array([
		[200000, 2000],
		[210000, 3000],
	])

	batch_jobs_data = generate_from_array(param_values, param_names, base_job_template)

	processor = BatchAbaqusProcessor(
		batch_data=batch_jobs_data,
		base_output_dir=OUTPUT_DIR,
		abaqus_exe=ABAQUS_CAE,
		cpus_per_job=CPU_PER_JOB,
	)

	results = processor.run_batch(num_parallel_jobs=BATCH_SIZE)

	print(results)