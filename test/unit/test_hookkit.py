"""Unit tests for hookkit (HK-01), no Abaqus required.

hookkit.py is staged into every job's output dir so hook scripts can
``import hookkit`` — these tests exercise it directly / via subprocess,
without ever launching Abaqus.

Run: pytest test/unit/test_hookkit.py -v
"""

import json
import os
import subprocess
import sys

import pytest

from ABQflow import extract_json


@pytest.fixture
def hookkit_module():
	"""Import hookkit from src/ABQflow/."""
	sys.path.insert(0, os.path.abspath('src/ABQflow'))
	import hookkit
	return hookkit


# -- T2: run() golden path -----------------------------------------------

def test_hookkit_run_golden_path(hookkit_module, temp_output_dir):
	"""run() with a fake extract_fn produces exactly one sentinel block."""
	# Write tasks JSON
	tasks_path = os.path.join(temp_output_dir, 'tasks.json')
	with open(tasks_path, 'w') as f:
		json.dump([{'result_name': 'alpha'}, {'result_name': 'beta'}], f)

	# Run hookkit.run via subprocess (simulates real usage)
	script = os.path.join(temp_output_dir, '_test_hook.py')
	with open(script, 'w') as f:
		f.write("""\
import os, sys
sys.path.insert(0, r'{hook_dir}')
import hookkit

def extract_one(source_path, task):
    name = task['result_name']
    if name == 'alpha':
        return 42.0
    if name == 'beta':
        return "hello"
    raise ValueError("unknown")

if __name__ == '__main__':
    hookkit.run(extract_one, source_arg='--src_path')
""".format(hook_dir=os.path.abspath('src/ABQflow')))

	cp = subprocess.run(
		[sys.executable, script, '--src_path', '/fake/src',
		 '--tasks_json', tasks_path],
		cwd=temp_output_dir, capture_output=True, text=True,
	)

	assert cp.returncode == 0
	stdout = cp.stdout
	assert '===ABQ_RESULT_BEGIN===' in stdout
	assert '===ABQ_RESULT_END===' in stdout

	results = extract_json(stdout)
	assert results == {'alpha': 42.0, 'beta': 'hello'}


# -- T3: partial failure --------------------------------------------------

def test_hookkit_run_partial_failure(hookkit_module, temp_output_dir):
	"""One task raises → that key = None, others OK, exit code 0."""
	tasks_path = os.path.join(temp_output_dir, 'tasks.json')
	with open(tasks_path, 'w') as f:
		json.dump([
			{'result_name': 'ok_task'},
			{'result_name': 'bad_task'},
			{'result_name': 'also_ok'},
		], f)

	script = os.path.join(temp_output_dir, '_test_hook.py')
	with open(script, 'w') as f:
		f.write("""\
import os, sys
sys.path.insert(0, r'{hook_dir}')
import hookkit

def extract_one(source_path, task):
    name = task['result_name']
    if name == 'bad_task':
        raise RuntimeError("simulated failure")
    return name.upper()

if __name__ == '__main__':
    hookkit.run(extract_one, source_arg='--src_path')
""".format(hook_dir=os.path.abspath('src/ABQflow')))

	cp = subprocess.run(
		[sys.executable, script, '--src_path', '/fake/src',
		 '--tasks_json', tasks_path],
		cwd=temp_output_dir, capture_output=True, text=True,
	)

	assert cp.returncode == 0
	results = extract_json(cp.stdout)
	assert results['ok_task'] == 'OK_TASK'
	assert results['bad_task'] is None
	assert results['also_ok'] == 'ALSO_OK'
	assert 'bad_task' in cp.stderr


# -- T5: field three modes ------------------------------------------------

def _make_field_task(result_name, output=None):
	task = {'result_name': result_name}
	if output is not None:
		task['output'] = output
	return task


def test_hookkit_field_inline(hookkit_module):
	"""mode='inline' always returns rows as-is, even for large data."""
	rows = [[i, float(i)] for i in range(15000)]  # above threshold
	result = hookkit_module.field(
		_make_field_task('test', 'inline'), rows, ['idx', 'val'])
	assert isinstance(result, list)
	assert len(result) == 15000


def test_hookkit_field_file(hookkit_module, temp_output_dir):
	"""mode='file' writes CSV + returns envelope, even for tiny data."""
	rows = [[1, 2.0], [3, 4.0]]
	os.chdir(temp_output_dir)  # field writes to cwd
	try:
		task = _make_field_task('tiny_field', 'file')
		task['_hookkit_job_name'] = 'job001'
		result = hookkit_module.field(task, rows, ['a', 'b'])
	finally:
		os.chdir(os.path.dirname(temp_output_dir))

	assert isinstance(result, dict)
	assert result['__file__'] == 'job001_tiny_field.csv'
	assert result['format'] == 'csv'
	assert result['shape'] == [2, 2]
	assert result['columns'] == ['a', 'b']
	assert os.path.isfile(os.path.join(temp_output_dir, 'job001_tiny_field.csv'))


def test_hookkit_field_auto_below_threshold(hookkit_module):
	"""mode='auto' with small data stays inline."""
	rows = [[i] for i in range(5)]
	result = hookkit_module.field(
		_make_field_task('small'), rows, ['x'], mode='auto')
	assert isinstance(result, list)


def test_hookkit_field_task_output_wins(hookkit_module, temp_output_dir):
	"""task['output']='inline' overrides mode='file'."""
	rows = [[1, 2.0]]
	os.chdir(temp_output_dir)
	try:
		task = _make_field_task('forced_inline', 'inline')
		result = hookkit_module.field(task, rows, ['a', 'b'], mode='file')
	finally:
		os.chdir(os.path.dirname(temp_output_dir))
	assert isinstance(result, list)  # task['output'] wins


# -- T4: emit idempotency ------------------------------------------------

def test_hookkit_emit_idempotent(hookkit_module, capsys):
	"""Second emit() raises RuntimeError."""
	hookkit_module.emit({'x': 1})
	with pytest.raises(RuntimeError, match='twice'):
		hookkit_module.emit({'y': 2})


# -- T7: job_name in file naming -----------------------------------------

def test_hookkit_field_no_job_name(hookkit_module, temp_output_dir):
	"""Without --job_name, file is named {result_name}.csv."""
	rows = [[1, 2.0]]
	os.chdir(temp_output_dir)
	try:
		task = _make_field_task('plain_field', 'file')
		result = hookkit_module.field(task, rows, ['a', 'b'])
	finally:
		os.chdir(os.path.dirname(temp_output_dir))

	assert result['__file__'] == 'plain_field.csv'


# -- scalar ---------------------------------------------------------------

def test_hookkit_scalar(hookkit_module):
	assert hookkit_module.scalar(42) == 42.0
	assert hookkit_module.scalar(3.14) == 3.14


# -- fail -----------------------------------------------------------------

def test_hookkit_fail(hookkit_module):
	assert hookkit_module.fail('t1', 'bad input') is None
