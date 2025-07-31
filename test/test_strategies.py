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

test_batch_job = [{
	'workflow': 'modular',
	'job_name': 'test_inp_based_job',
	# If using InpModifyStrategy:
	'type': 'inp_based',
	'base_inp_path': './test/test_file/planar_stress_template.inp',
	# If using ModelGenerationStrategy:
	# 'type': 'model_generation',
	# 'model_script_path': './test/test_file/model_generation.py',
	'params': {'youngs_modulus': 200000, 'load_magnitude': 2000},
	
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
}]

CPU_PER_JOB = 4
BATCH_SIZE = 2
ABAQUS_CAE = 'C:/Applications/SIMULIA/Commands/abaqus.bat'
OUTPUT_DIR = "C:/SJTU/Projects_Code/24_Abaqus_Pack/test/output"


@pytest.mark.skip
def test_total():
	processor = BatchAbaqusProcessor(
		batch_data=test_batch_job,
		base_output_dir=OUTPUT_DIR,
		abaqus_exe=ABAQUS_CAE,
		cpus_per_job= CPU_PER_JOB,
	)
	results = processor.run_batch(num_parallel_jobs=BATCH_SIZE)
	results1 = processor.run_batch(num_parallel_jobs=BATCH_SIZE, output_type='dict')
	print(results)
	print(results1)
