# Note: This file is handed to abaqus python interpreter, so it should not import any ABQflow modules.

import sys
import argparse
import json
from odbAccess import openOdb

RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END = "===ABQ_RESULT_END==="

def get_max_misesstress_displacement(odb_path, tasks_json_path):
	"""
	Args:
		odb_path (`str`): Path to the ODB file.
		tasks_json_path (`str`): Path to the JSON file containing tasks.

	Raises:
		OdbError: 如果 ODB 文件损坏或无法打开。
		KeyError: 如果指定的分析步或实例名称不存在。
		IndexError: 如果指定的节点标签或分量不存在。

	Print:
		输出最大 Mises 应力值和对应的元素标签。
	"""
	try:
		with open(tasks_json_path, 'r', encoding='utf-8') as f: 
			task_list = json.load(f)

		odb = openOdb(path=odb_path)
		results = {}

		for task in task_list:
			result_name = task['result_name']
			try:
				value = extract_result(odb, result_name)
				results[result_name] = value
			except Exception as e:
				results[result_name] = None
				sys.__stderr__.write(f"  - Sub-task '{result_name}' failed: {e}\n")

		odb.close()
		# sys.__stdout__.write(json.dumps(results, indent=4) + "\n")
		sys.__stdout__.write(f"{RESULT_BEGIN}\n{json.dumps(results, indent=4)}\n{RESULT_END}\n")

	except Exception as e:
		sys.__stderr__.write(f"Fatal error in get_max_stress_mises.py: {e}\n")
		sys.exit(1)

def extract_result(odb, task):
	step_n = odb.steps['Elastic']
	lastframe = step_n.frames[-1]
	mdb = odb.rootAssembly

	if task == 'max_stress_mises':
		get_stress = lastframe.fieldOutputs['S']
		stress = get_stress.getSubset(region=mdb.elementSets[' ALL ELEMENTS']).values
		max_stress = max(stress, key=lambda s: s.mises)
		value = max_stress.mises
	elif task == 'max_displacement':
		get_displacement = lastframe.fieldOutputs['U']
		displacement = get_displacement.getSubset(region=mdb.nodeSets[' ALL NODES']).values
		max_displacement = max(displacement, key=lambda u: u.magnitude)
		value = max_displacement.magnitude
	else:
		raise ValueError(f"Unsupported task: {task}")
	
	return value


if __name__ == "__main__":
	parser = argparse.ArgumentParser(description="Extract maximum Mises stress from ODB file.")
	parser.add_argument('--odb_path', type=str, required=True)
	parser.add_argument('--tasks_json', type=str, required=True)

	args, unknown = parser.parse_known_args()
	get_max_misesstress_displacement(args.odb_path, args.tasks_json)