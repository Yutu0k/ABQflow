# Note: This file is handed to abaqus python interpreter, so it should not import any ABQflow modules.

import sys
import argparse
import json
from abaqus import mdb

RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END = "===ABQ_RESULT_END==="


def get_total_mass(inp_path, tasks_json_path):
	try:
		with open(tasks_json_path, 'r', encoding='utf-8') as f:
			task_list = json.load(f)

			mdb.ModelFromInputFile(name='test', inputFileName=inp_path)
			results = {}

			if 'Model-1' in mdb.models:
				del mdb.models['Model-1']

			for task in task_list:
				result_name = task['result_name']
				try:
					value = extract_mass(mdb, result_name)
					results[result_name] = value
				except Exception as e:
					results[result_name] = None
					sys.__stderr__.write(f"  - Sub-task '{result_name}' failed: {e}\n")

				mdb.close()
				# sys.__stdout__.write(json.dumps(results, indent=4) + "\n")
				sys.__stdout__.write(f"{RESULT_BEGIN}\n{json.dumps(results, indent=4)}\n{RESULT_END}\n")
				
	except Exception as e:
		sys.__stderr__.write(f"Fatal error in get_total_mass.py: {e}\n")
		sys.exit(1)	

def extract_mass(mdb, task):
	root_assembly = mdb.models['test'].rootAssembly
	region = root_assembly.sets['ALL'].elements

	if task == 'total_mass':
		GetMass = root_assembly.getMassProperties(regions=region)
		mass = GetMass['mass']

	return mass


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Extract total mass from INP file.")
	parser.add_argument('--inp_path', type=str, required=True)
	parser.add_argument('--tasks_json', type=str, required=True)

	args, unknown = parser.parse_known_args()
	get_total_mass(args.inp_path, args.tasks_json)