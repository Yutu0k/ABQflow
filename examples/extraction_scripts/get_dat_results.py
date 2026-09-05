# -*- coding: utf-8 -*-
# get_dat_results.py — runs under the host python, not abaqus python.
#
# Declare it with source='dat' and ABQflow reads <job>.dat instead of the ODB:
#
#     HookSpec(script_path="./examples/extraction_scripts/get_dat_results.py",
#              source="dat",
#              tasks=[{"result_name": "max_stress_mises"},
#                     {"result_name": "mises_field", "output": "file"}])
#
# The deck needs a matching print request, e.g.
#     *EL PRINT, ELSET=ALL, POSITION=INTEGRATION POINTS, SUMMARY=YES
#     MISES
import os
import sys
sys.path.insert(0, os.getcwd())     # hookkit + datkit are staged here by ABQflow
import hookkit
import datkit


def extract_one(dat_path, task):
	"""The ONLY thing the user writes: physics in, value out.
	Raise on failure — hookkit converts it to None + stderr log."""
	name = task['result_name']

	doc = datkit.parse(dat_path)
	# *EL PRINT emits one table per element type (CPS3, CPS4R, ...); take them all.
	tables = datkit.select_tables(doc, kind='element', increment='last')
	header, rows = datkit.to_rows(tables, columns=['MISES'])

	if name == 'max_stress_mises':
		return hookkit.scalar(datkit.reduce_rows(rows, header, 'MISES', 'max'))
	if name == 'mises_field':
		return hookkit.field(task, rows, header)
	raise ValueError("unsupported result_name: %s" % name)


if __name__ == '__main__':
	hookkit.run(extract_one, source_arg='--dat_path')
