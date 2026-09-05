"""The example ``.dat`` hook — end to end, no Abaqus required.

``examples/extraction_scripts/get_dat_results.py`` is what a
``HookSpec(source='dat')`` points at.  These tests run it exactly as ABQflow
does: ``hookkit.py`` and ``datkit.py`` copied into the job directory, a tasks
JSON alongside, and the host Python as the interpreter.

Run: pytest test/unit/test_dat_hook.py -v
"""

from __future__ import annotations

import csv
import json
import os
import shutil
import subprocess
import sys

import pytest

from ABQflow import extract_json

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SRC = os.path.join(_REPO, 'src', 'ABQflow')
FIXTURES = os.path.join(_REPO, 'test', 'fixtures', 'dat')
HOOK = os.path.join(_REPO, 'examples', 'extraction_scripts', 'get_dat_results.py')

# Real Abaqus 2026 *EL PRINT output: 5 CPS3 + 8 CPS4R rows survive the trim.
EL_PRINT = 'el_print_mixed.dat'
EXPECTED_MAX = 4473.0
EXPECTED_ROWS = 13


def _stage(job_dir, tasks, fixture=EL_PRINT):
	"""Lay out a job directory the way AbaqusRunner._stage_hookkit does."""
	for name in ('hookkit.py', 'datkit.py'):
		shutil.copy2(os.path.join(_SRC, name), os.path.join(job_dir, name))
	dat_path = os.path.join(job_dir, 'job001.dat')
	shutil.copy2(os.path.join(FIXTURES, fixture), dat_path)

	tasks_path = os.path.join(job_dir, 'tasks.json')
	with open(tasks_path, 'w', encoding='utf-8') as f:
		json.dump(tasks, f)
	return dat_path, tasks_path


def _run(job_dir, dat_path, tasks_path, job_name='job001'):
	return subprocess.run(
		[sys.executable, HOOK, '--dat_path', dat_path,
		 '--tasks_json', tasks_path, '--job_name', job_name],
		cwd=job_dir, capture_output=True, text=True,
	)


def test_hook_script_exists():
	assert os.path.isfile(HOOK)


def test_scalar_and_field(tmp_path):
	job_dir = str(tmp_path)
	dat_path, tasks_path = _stage(job_dir, [
		{'result_name': 'max_stress_mises'},
		{'result_name': 'mises_field', 'output': 'file'},
	])
	proc = _run(job_dir, dat_path, tasks_path)

	assert proc.returncode == 0, proc.stderr
	assert proc.stdout.count('===ABQ_RESULT_BEGIN===') == 1
	results = extract_json(proc.stdout)

	assert results['max_stress_mises'] == pytest.approx(EXPECTED_MAX)

	envelope = results['mises_field']
	assert envelope['__file__'] == 'job001_mises_field.csv'
	assert envelope['columns'] == ['ELEMENT', 'PT', 'MISES']
	assert envelope['shape'] == [EXPECTED_ROWS, 3]

	csv_path = os.path.join(job_dir, envelope['__file__'])
	assert os.path.getsize(csv_path) > 0
	with open(csv_path, newline='', encoding='utf-8') as f:
		rows = list(csv.reader(f))
	assert rows[0] == envelope['columns']
	assert len(rows) == EXPECTED_ROWS + 1
	# Every cell must coerce to a float or helpers.convert.load_field breaks.
	for row in rows[1:]:
		for cell in row:
			float(cell)


def test_field_defaults_to_inline_for_a_small_table(tmp_path):
	"""No ``output`` key means hookkit's auto mode, and 13 rows stay inline."""
	job_dir = str(tmp_path)
	dat_path, tasks_path = _stage(job_dir, [{'result_name': 'mises_field'}])
	proc = _run(job_dir, dat_path, tasks_path)

	assert proc.returncode == 0, proc.stderr
	rows = extract_json(proc.stdout)['mises_field']
	assert isinstance(rows, list)
	assert len(rows) == EXPECTED_ROWS
	assert rows[0] == [1, 1, 2914.0]


def test_both_element_types_are_included(tmp_path):
	"""*EL PRINT emits one table per element type; the hook must read them all."""
	job_dir = str(tmp_path)
	dat_path, tasks_path = _stage(job_dir, [{'result_name': 'mises_field',
											'output': 'inline'}])
	proc = _run(job_dir, dat_path, tasks_path)
	rows = extract_json(proc.stdout)['mises_field']

	labels = [r[0] for r in rows]
	assert labels[:5] == [1, 2, 3, 4, 5]        # CPS3 table
	assert 51 in labels                         # CPS4R table


def test_unsupported_result_name_is_none_with_a_diagnostic(tmp_path):
	"""Partial failure: one bad task must not take the others down."""
	job_dir = str(tmp_path)
	dat_path, tasks_path = _stage(job_dir, [
		{'result_name': 'max_stress_mises'},
		{'result_name': 'no_such_result'},
	])
	proc = _run(job_dir, dat_path, tasks_path)

	assert proc.returncode == 0
	results = extract_json(proc.stdout)
	assert results['max_stress_mises'] == pytest.approx(EXPECTED_MAX)
	assert results['no_such_result'] is None
	assert 'no_such_result' in proc.stderr


def test_missing_dat_fails_every_task_without_crashing(tmp_path):
	job_dir = str(tmp_path)
	_dat_path, tasks_path = _stage(job_dir, [{'result_name': 'max_stress_mises'}])
	proc = _run(job_dir, os.path.join(job_dir, 'nope.dat'), tasks_path)

	assert proc.returncode == 0
	assert extract_json(proc.stdout) == {'max_stress_mises': None}
	assert 'nope.dat' in proc.stderr


def test_a_dat_without_element_output_fails_the_task(tmp_path):
	"""The Riks fixture is *NODE PRINT only — no MISES column to reduce."""
	job_dir = str(tmp_path)
	dat_path, tasks_path = _stage(job_dir, [{'result_name': 'max_stress_mises'}],
									fixture='riks_complete.dat')
	proc = _run(job_dir, dat_path, tasks_path)

	assert proc.returncode == 0
	assert extract_json(proc.stdout) == {'max_stress_mises': None}
	assert 'max_stress_mises' in proc.stderr
