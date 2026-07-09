"""Reference hook demonstrating the sidecar CSV protocol.

Overview
--------
When a hook extracts *large* data from an ODB (e.g. a stress field with 100 000+
values), stuffing it into stdout JSON will blow up the pipe and bloat JobOutcome.
The sidecar protocol solves this: the hook writes the data to a CSV file and
returns a lightweight *envelope* through stdout instead.

Threshold (any one triggers sidecar)
------------------------------------
- ``len(data) > 10 000``  (value count)
- ``len(json.dumps(data)) > 1 MB``  (serialized size)

Envelope format
---------------
::

	{
		"__file__": "job_name_result_name.csv",   // relative to cwd (= output_dir)
		"format":   "csv",
		"shape":    [184320, 4],
		"columns":  ["label", "s11", "s22", "s12"]
	}

Rules
-----
1. CSV **must** have a header row matching ``columns``.
2. ``__file__`` is a reserved key — normal result dicts must not use it.
3. Write the file first, then output the envelope ("promise after write").
4. On failure, output ``None`` — never an envelope pointing to a missing file.
5. Small data still goes through stdout JSON as before (backward compatible).

Usage as a hook
---------------
This script follows the standard ABQflow hook protocol.  It accepts
``--odb_path`` and ``--tasks_json``, and prints its results wrapped in
``===ABQ_RESULT_BEGIN===`` / ``===ABQ_RESULT_END===`` sentinels.

Test without Abaqus
-------------------
::

	python examples/reference_odb_hook.py --output_dir /tmp/test_sidecar

"""

from __future__ import annotations

import json
import os
import sys
import argparse
from odbAccess import openOdb
import numpy as np

RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END = "===ABQ_RESULT_END==="
_SIDECAR_KEY = "__file__"

# Thresholds: first one hit triggers sidecar
_SIDECAR_COUNT_THRESHOLD = 10_000
_SIDECAR_SIZE_THRESHOLD = 1_000_000  # 1 MB

def _extract_from_odb(odb_path: str, result_name: str) -> list | None:
	"""
	Simulate extracting data from an ODB.

	In a real hook, replace this with actual ``odbAccess`` calls.

	Should return list[list] or np.ndarray for large data
	"""
	if result_name == 'stress_mises':
		odb = openOdb(path=odb_path)

		step = odb.steps['Step-1']
		frame = step.frames[-1]
		mdb = odb.rootAssembly

		get_stress = frame.fieldOutputs['S']
		stress = get_stress.getSubset(region=mdb.elementSets[' ALL ELEMENTS']).values
		elemlabel_stress = [[s.elementLabel, s.mises] for s in stress]

		odb.close()
		return elemlabel_stress

	return None


def process_tasks(tasks: list[dict], odb_path: str, output_dir: str) -> dict:
	"""Execute all tasks and return ``{result_name: value_or_envelope}``."""
	results: dict = {}

	for task in tasks:
		name = task['result_name']
		raw = _extract_from_odb(odb_path, name)

		if raw is None:
			results[name] = None
			continue

		if should_use_sidecar(raw):
			file_name = f"{name}.csv"
			results[name] = write_sidecar_csv(raw, output_dir, file_name, columns=task.get('columns', ['value']))
		else:
			results[name] = raw

	return results


def should_use_sidecar(data) -> bool:
	"""
	Return True if *data* is too large for stdout JSON.

	Checks two conditions (first match wins):
	1. ``len(data) > 10 000`` (for list/array-like data)
	2. ``len(json.dumps(data)) > 1 MB``
	"""
	try:
		n = len(data)
	except TypeError:
		n = 0
	if n > _SIDECAR_COUNT_THRESHOLD:
		return True
	try:
		size = len(json.dumps(data, default=str))
	except (TypeError, ValueError):
		return False
	return size > _SIDECAR_SIZE_THRESHOLD

def write_sidecar_csv(
	data: list[list] | 'np.ndarray',
	output_dir: str,
	file_name: str,
	columns: list[str],
) -> dict:
	"""Write *data* as a CSV sidecar and return the envelope.

	The file is written **before** the envelope is returned
	("promise after write").

	Parameters
	----------
	data : list[list] or numpy.ndarray
		Row-major data to write.
	output_dir : str
		Directory to write the CSV into (hook's cwd).
	file_name : str
		CSV file name, e.g. ``"job_0001_stress.csv"``.
	columns : list[str]
		Column headers (CSV header row).

	Returns
	-------
	dict
		Sidecar envelope: ``{"__file__": ..., "format": "csv", ...}``.
		Ready to be included in the hook's result dict.
	"""
	os.makedirs(output_dir, exist_ok=True)
	csv_path = os.path.join(output_dir, file_name)
	n_rows = len(data)
	n_cols = len(data[0]) if n_rows > 0 else 0

	with open(csv_path, 'w', newline='', encoding='utf-8') as f:
		f.write(','.join(columns) + '\n')
		for row in data:
			f.write(','.join(str(v) for v in row) + '\n')

	return {
		_SIDECAR_KEY: file_name,
		'format': 'csv',
		'shape': [n_rows, n_cols],
		'columns': columns,
	}

if __name__ == '__main__':
	parser = argparse.ArgumentParser(description="Extract full field stress mises from ODB file")
	parser.add_argument('--odb_path', type=str, required=True)
	parser.add_argument('--tasks_json', type=str, default=None)

	parser.add_argument('--output_dir', default=os.getcwd(), help="Output directory (default: cwd)")
	parser.add_argument('--debug', action='store_true', help="Enable debug output")
	parser.add_argument('--debug_json_string', type=json.loads, help="Debug: pass JSON string directly instead of reading from file")

	
	args, unknown = parser.parse_known_args()

	if args.tasks_json is not None:
		with open(args.tasks_json, 'r') as f:
			tasks = json.load(f)
	else:
		if args.debug:
			sys.__stdout__.write(f"Debug: args={args}\n")
			if args.debug_json_string:
				tasks = [args.debug_json_string]
				sys.__stdout__.write(f"Debug: debug_json_string={args.debug_json_string}\n")
			else:
				raise ValueError("Debug mode requires --debug_json_string to be provided")

	results = process_tasks(tasks=tasks, odb_path=args.odb_path, output_dir=args.output_dir)

	payload = json.dumps(results, default=str)
	sys.stdout.write(f"{RESULT_BEGIN}\n{payload}\n{RESULT_END}\n")