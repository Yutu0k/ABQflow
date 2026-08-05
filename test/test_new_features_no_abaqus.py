"""Tests for new ABQflow features (IMP-01 through IMP-06) that do NOT require Abaqus.

Run: pytest test/test_new_features_no_abaqus.py -v
"""

import json
import logging
import os
import tempfile

import numpy as np
import pytest

from ABQflow import (
	SolverDiagnostics, SolverResult,
	parse_sta, harvest_errors, diagnose, apply_truth_table,
	JobSpec, PreparationSpec, HookSpec, SubroutineSpec,
	BatchAbaqusProcessor, JobOutcome, JobPlan, CommandRecord,
	is_sidecar, resolve_sidecar,
	load_field, iter_fields,
	degenerate_from_array,
	build_workflow,
	outcomes_to_list, outcomes_to_dict,
	JobStatus,
	AbaqusCalculation, AbaqusRunner, JobContext,
	ModularWorkflowStrategy, MonolithicWorkflowStrategy, PreparationStrategy,
	OdbExtractionStrategy, SubroutineCompileStrategy,
)
from ABQflow.core.status import _TERMINAL_FAILURES, JobStatusManager


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def real_sta_files():
	"""All .sta files in test/output/ (pre-computed, COMPLETED Standard jobs)."""
	import glob
	files = sorted(glob.glob('test/output/**/*.sta', recursive=True))
	if not files:
		pytest.skip("No real .sta files found in test/output/")
	return files


# ============================================================
# 1. Truth table (IMP-02)
# ============================================================

def test_truth_table():
	"""All 5 rows of the truth table produce correct verdicts."""
	assert apply_truth_table(0, 'COMPLETED') == (True, None)
	assert apply_truth_table(0, 'NOT_COMPLETED') == (False, None)
	assert apply_truth_table(0, 'ABORTED') == (False, None)
	assert apply_truth_table(0, 'INDETERMINATE') == (False, None)
	ok, warn = apply_truth_table(1, 'COMPLETED')
	assert ok is True and warn is not None
	assert apply_truth_table(1, 'NOT_COMPLETED') == (False, None)


# ============================================================
# 2. STA parsing
# ============================================================

def test_parse_sta_real_file(real_sta_files):
	"""Real .sta files from test/output/ parse correctly."""
	for f in real_sta_files:
		verdict, increments, solver_type = parse_sta(f)
		assert verdict == 'COMPLETED', f"Expected COMPLETED, got {verdict} for {f}"
		assert increments >= 1, f"Expected >=1 increments in {f}"
		assert solver_type == 'standard', f"Expected standard, got {solver_type} for {f}"


def test_parse_sta_missing_file():
	"""Missing .sta returns INDETERMINATE."""
	v, inc, st = parse_sta('/nonexistent/file.sta')
	assert v == 'INDETERMINATE'
	assert inc == 0
	assert st == 'unknown'


# ============================================================
# 3. Error harvesting
# ============================================================

def test_harvest_errors_dedup():
	"""Consecutive identical errors are folded with repeat count."""
	with tempfile.TemporaryDirectory() as d:
		msg = os.path.join(d, 'test.msg')
		with open(msg, 'w') as f:
			f.write("ERROR: Convergence issue\n" * 5)
			f.write("WARNING: Small time increment\n")
			f.write("ERROR: Different error\n")

		errors, et, wt = harvest_errors(msg, None, k_errors=5, k_chars=500)
		assert et == 6
		assert wt == 1
		assert len(errors) == 2
		assert 'repeated 5 times' in errors[0]
		assert 'Different error' in errors[1]


def test_harvest_errors_truncation():
	"""Errors beyond k_errors are counted but not stored."""
	with tempfile.TemporaryDirectory() as d:
		msg = os.path.join(d, 'test.msg')
		with open(msg, 'w') as f:
			for i in range(10):
				f.write(f"ERROR: Error number {i}\n")

		errors, et, wt = harvest_errors(msg, None, k_errors=3)
		assert et == 10
		assert len(errors) == 3


def test_harvest_errors_no_files():
	"""No files → empty result."""
	errors, et, wt = harvest_errors(None, None)
	assert errors == []
	assert et == 0
	assert wt == 0


# ============================================================
# 4. diagnose
# ============================================================

def test_diagnose_empty_dir():
	"""Empty directory → INDETERMINATE with no errors."""
	with tempfile.TemporaryDirectory() as d:
		diag = diagnose('test_job', d)
		assert diag.sta_verdict == 'INDETERMINATE'
		assert diag.errors == []
		assert diag.error_total == 0
		assert diag.source_files == {}


# ============================================================
# 5. Dataclass defaults
# ============================================================

def test_solver_diagnostics_defaults():
	d = SolverDiagnostics()
	assert d.sta_verdict == 'INDETERMINATE'
	assert d.errors == []
	assert d.source_files == {}
	assert d.solver_type == 'unknown'


def test_solver_result():
	r = SolverResult(success=True)
	assert r.success and r.error is None and r.diagnostics is None

	r2 = SolverResult(success=False, error="test",
					diagnostics=SolverDiagnostics(sta_verdict='ABORTED'))
	assert not r2.success
	assert r2.error == "test"
	assert r2.diagnostics.sta_verdict == 'ABORTED'


# ============================================================
# 6. Preflight (IMP-04)
# ============================================================

def test_jobspec_preflight_validation():
	for v in ('syntaxcheck', 'datacheck', None):
		spec = JobSpec('j', workflow='monolithic', monolithic_script='t.py', preflight=v)
		assert spec.preflight == v

	with pytest.raises(ValueError, match='preflight'):
		JobSpec('j', workflow='monolithic', monolithic_script='t.py', preflight='bad')


def test_preflight_failed_status():
	assert JobStatus.PREFLIGHT_FAILED in _TERMINAL_FAILURES

	mgr = JobStatusManager()
	mgr.record_preflight(success=False, error="Bad INP")
	assert mgr.get_final_status() == JobStatus.PREFLIGHT_FAILED
	assert mgr.error_message == "Bad INP"

	# First-failure-wins: subsequent transition is no-op
	mgr.record_simulation(success=True)
	assert mgr.get_final_status() == JobStatus.PREFLIGHT_FAILED


def test_workflow_preflight_mode():
	spec = JobSpec('pf_test',
				preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'),
				preflight='syntaxcheck')
	wf = build_workflow(spec)
	assert wf.preflight_mode == 'syntaxcheck'
	assert wf.preflight_only is False

	wf2 = build_workflow(spec, preflight_only=True)
	assert wf2.preflight_only is True


def test_batch_processor_preflight_only():
	spec = JobSpec('pf', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'))
	bp = BatchAbaqusProcessor([spec], './test_pf', cpus_per_job=4, preflight_only=True)
	assert bp.preflight_only is True


# ============================================================
# 7. Dry-run (IMP-05)
# ============================================================

def test_dry_run_plan():
	spec = JobSpec('dry_test',
				preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'),
				preflight='syntaxcheck')
	bp = BatchAbaqusProcessor([spec], './test_dry_plan', cpus_per_job=4)
	plans = bp.dry_run('plan')
	assert len(plans) == 1
	p = plans[0]
	assert p.job_name == 'dry_test'
	assert len(p.commands) == 2  # preflight + solver
	assert p.commands[0].stage == 'preflight'
	assert p.commands[1].stage == 'solver'
	assert 'cpus_per_job' in p.resource_summary
	assert 'tokens_per_job' in p.resource_summary
	# L1: zero file-system side effects
	assert not os.path.isdir('./test_dry_plan/dry_test')


def test_dry_run_bad_level():
	bp = BatchAbaqusProcessor(
		[JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'))],
		'./test_dry', cpus_per_job=4)
	with pytest.raises(ValueError, match='plan'):
		bp.dry_run('invalid')


def test_command_record_and_job_plan():
	cr = CommandRecord('solver', ['abaqus', 'job=test'], '/tmp')
	assert cr.stage == 'solver'
	assert cr.cmd == ['abaqus', 'job=test']

	p = JobPlan('test_job', commands=[cr],
				paths={'inp': '/t.inp', 'odb': '/t.odb'},
				resource_summary={'cpus_per_job': 4})
	assert p.job_name == 'test_job'
	assert len(p.commands) == 1


# ============================================================
# 8. Sidecar contract (IMP-06)
# ============================================================

def test_is_sidecar():
	assert is_sidecar({'__file__': 'test.csv', 'format': 'csv'})
	assert not is_sidecar({'normal': 'dict'})
	assert not is_sidecar([1, 2, 3])
	assert not is_sidecar(None)
	assert not is_sidecar("string")


def test_resolve_sidecar():
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'data.csv')
		with open(csv_path, 'w') as f:
			f.write("col1,col2\n1.0,2.0\n3.0,4.0\n")

		envelope = {'__file__': 'data.csv', 'format': 'csv', 'shape': [2, 2]}

		abspath, meta = resolve_sidecar(envelope, d, load=False)
		assert abspath == csv_path
		assert meta == {'format': 'csv', 'shape': [2, 2]}

		data, meta = resolve_sidecar(envelope, d, load=True)
		assert data.shape == (2, 2)
		assert data[0, 0] == 1.0


def test_degenerate_sidecar_guard():
	oc = JobOutcome('test', 'COMPLETED', results={'big': {'__file__': 'f.csv'}})
	with pytest.raises(ValueError, match='sidecar field'):
		degenerate_from_array([oc], ['big'])


# ============================================================
# 8b. SC-01: sidecar field loading (load_field / iter_fields)
# ============================================================

@pytest.fixture
def csv_outcome():
	"""Create a JobOutcome with both inline and sidecar results."""
	with tempfile.TemporaryDirectory() as d:
		# Write a sidecar CSV
		csv_path = os.path.join(d, 'job001_stress.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("x,y,z\n1.0,2.0,3.0\n4.0,5.0,6.0\n7.0,8.0,9.0\n")

		oc = JobOutcome(
			job_name='job001',
			status='COMPLETED',
			results={
				'mass': 0.42,
				'stress': {
					'__file__': 'job001_stress.csv',
					'format': 'csv',
					'shape': [3, 3],
					'columns': ['x', 'y', 'z'],
				},
			},
			output_dir=d,
		)
		yield oc


# -- T1: dual-representation normalisation -----------------------------------

def test_load_field_inline(csv_outcome):
	"""Inline scalar is returned as ndarray."""
	arr = load_field(csv_outcome, 'mass')
	assert isinstance(arr, np.ndarray)
	assert arr.shape == ()
	assert arr.item() == 0.42


def test_load_field_sidecar(csv_outcome):
	"""Sidecar envelope loads CSV into ndarray."""
	arr = load_field(csv_outcome, 'stress')
	assert isinstance(arr, np.ndarray)
	assert arr.shape == (3, 3)
	assert arr[0, 0] == 1.0
	assert arr[2, 2] == 9.0


def test_load_field_missing_result(csv_outcome):
	"""Missing result_name returns None."""
	assert load_field(csv_outcome, 'nonexistent') is None


def test_load_field_none_results():
	"""Outcome with results=None returns None."""
	oc = JobOutcome('j', 'COMPLETED', results=None)
	assert load_field(oc, 'anything') is None


def test_load_field_no_output_dir():
	"""Outcome without output_dir returns None + warning."""
	oc = JobOutcome('j', 'COMPLETED', results={'f': {'__file__': 'x.csv'}})
	with pytest.warns(UserWarning, match='no output_dir'):
		assert load_field(oc, 'f') is None


# -- T3: iter_fields on_missing modes ----------------------------------------

def _make_ocs(d):
	"""Helper: create outcomes from {name: results_or_None}."""
	ocs = []
	for name, results in [('job_2', {'v': [1.0, 2.0]}),
						  ('job_1', {'v': [3.0, 4.0]}),
						  ('job_10', {'v': None})]:
		ocs.append(JobOutcome(name, 'COMPLETED', results=results))
	return ocs


def test_iter_fields_natural_sort():
	"""iter_fields yields in natural key order (job_1 before job_2 before job_10)."""
	ocs = _make_ocs(None)
	# Use on_missing='none' so job_10 (v=None) is included
	names = [n for n, _ in iter_fields(ocs, 'v', on_missing='none')]
	assert names == ['job_1', 'job_2', 'job_10']


def test_iter_fields_on_missing_skip():
	"""on_missing='skip' omits missing jobs + issues summary warning."""
	ocs = _make_ocs(None)
	with pytest.warns(UserWarning, match='skipped due to missing'):
		results = list(iter_fields(ocs, 'v', on_missing='skip'))
	assert len(results) == 2
	assert results[0][0] == 'job_1'
	assert np.array_equal(results[0][1], [3.0, 4.0])


def test_iter_fields_on_missing_none():
	"""on_missing='none' yields (name, None) for missing jobs."""
	ocs = _make_ocs(None)
	results = list(iter_fields(ocs, 'v', on_missing='none'))
	assert len(results) == 3
	assert results[2] == ('job_10', None)


def test_iter_fields_on_missing_raise():
	"""on_missing='raise' raises on first missing field."""
	ocs = _make_ocs(None)
	with pytest.raises(ValueError, match="missing for job 'job_10'"):
		list(iter_fields(ocs, 'v', on_missing='raise'))


def test_iter_fields_bad_on_missing():
	"""Invalid on_missing value raises ValueError."""
	with pytest.raises(ValueError, match="on_missing must be"):
		list(iter_fields([], 'x', on_missing='bad'))


# -- T4: migration scenario ---------------------------------------------------

def test_load_field_missing_csv():
	"""File referenced by sidecar no longer exists → None + warning."""
	with tempfile.TemporaryDirectory() as d:
		oc = JobOutcome('j', 'COMPLETED',
			results={'f': {'__file__': 'gone.csv', 'format': 'csv'}},
			output_dir=d)
		with pytest.warns(UserWarning, match='missing or empty'):
			assert load_field(oc, 'f') is None


# -- T5: numeric_only + shape mismatch ---------------------------------------

def test_load_field_numeric_only_drops_string_col():
	"""Non-numeric column is dropped with warning when numeric_only=True."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'mixed.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("label,val\nabc,1.0\ndef,2.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'mixed': {'__file__': 'mixed.csv', 'format': 'csv'}},
			output_dir=d)
		with pytest.warns(UserWarning, match="Non-numeric column"):
			arr = load_field(oc, 'mixed')
		assert arr.shape == (2, 1)  # only 'val' column survives
		assert arr[0, 0] == 1.0


def test_load_field_numeric_only_false():
	"""numeric_only=False preserves string columns as object array."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'mixed.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("label,val\nabc,1.0\ndef,2.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'mixed': {'__file__': 'mixed.csv', 'format': 'csv'}},
			output_dir=d)
		arr = load_field(oc, 'mixed', numeric_only=False)
		assert arr.shape == (2, 2)
		assert arr[0, 0] == 'abc'
		assert arr[0, 1] == '1.0'


def test_load_field_shape_mismatch_warns():
	"""Claimed shape differs from file → warning, data still loaded."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'data.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("a,b\n1.0,2.0\n3.0,4.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'f': {'__file__': 'data.csv', 'format': 'csv',
						   'shape': [999, 2]}},
			output_dir=d)
		with pytest.warns(UserWarning, match='shape mismatch'):
			arr = load_field(oc, 'f')
		assert arr.shape == (2, 2)


# -- T2: row-order contract ---------------------------------------------------

def test_iter_fields_aligns_with_degenerate():
	"""iter_fields('none') row i corresponds to degenerate row i (same job)."""
	with tempfile.TemporaryDirectory() as d:
		ocs = [
			JobOutcome('job_2', 'COMPLETED',
				results={'mass': 2.0},
				output_dir=d),
			JobOutcome('job_1', 'COMPLETED',
				results={'mass': 1.0},
				output_dir=d),
			JobOutcome('job_3', 'COMPLETED',
				results={'mass': 3.0},
				output_dir=d),
		]
		# degenerate sorts by natural key (job_1, job_2, job_3)
		mat = degenerate_from_array(ocs, ['mass'])
		assert mat[0, 0] == 1.0
		assert mat[1, 0] == 2.0
		assert mat[2, 0] == 3.0

		# iter_fields('none') uses same sort → same order
		fields = list(iter_fields(ocs, 'mass', on_missing='none'))
		assert fields[0][0] == 'job_1'
		assert fields[1][0] == 'job_2'
		assert fields[2][0] == 'job_3'


# ============================================================
# 9. JobOutcome.diagnostics pass-through
# ============================================================

def test_job_outcome_diagnostics():
	oc = JobOutcome('j1', 'COMPLETED')
	assert oc.diagnostics is None

	oc2 = JobOutcome('j2', 'SIMULATION_FAILED', error='err',
					diagnostics={'sta_verdict': 'NOT_COMPLETED', 'errors': ['x']})
	assert oc2.diagnostics['sta_verdict'] == 'NOT_COMPLETED'


def test_outcomes_serialization_diagnostics():
	oc = JobOutcome('j1', 'FAILED', error='err',
					diagnostics={'sta_verdict': 'ABORTED'},
					results={'x': 1.0})

	lst = outcomes_to_list([oc])
	assert lst[0]['diagnostics'] == {'sta_verdict': 'ABORTED'}
	assert lst[0]['x'] == 1.0

	dct = outcomes_to_dict([oc])
	assert dct['j1']['diagnostics'] == {'sta_verdict': 'ABORTED'}


# ============================================================
# 10. hookkit unit tests (HK-01, no Abaqus required)
# ============================================================

@pytest.fixture
def hookkit_module():
	"""Import hookkit from src/ABQflow/."""
	import sys
	sys.path.insert(0, os.path.abspath('src/ABQflow'))
	import hookkit
	return hookkit


@pytest.fixture
def temp_output_dir():
	"""Temporary directory for hookkit sidecar output."""
	with tempfile.TemporaryDirectory() as d:
		yield d


# -- T2: run() golden path -----------------------------------------------

def test_hookkit_run_golden_path(hookkit_module, temp_output_dir):
	"""run() with a fake extract_fn produces exactly one sentinel block."""
	import subprocess, sys

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

	from ABQflow import extract_json
	results = extract_json(stdout)
	assert results == {'alpha': 42.0, 'beta': 'hello'}


# -- T3: partial failure --------------------------------------------------

def test_hookkit_run_partial_failure(hookkit_module, temp_output_dir):
	"""One task raises → that key = None, others OK, exit code 0."""
	import subprocess, sys

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
	from ABQflow import extract_json
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


# ============================================================
# 11. Runner envelope validation tests (HK-01 §3.6)
# ============================================================

@pytest.fixture
def dummy_logger():
	return logging.getLogger('test_validate')


def test_validate_envelope_path_escape(dummy_logger, temp_output_dir):
	"""Envelope with ../ path → None."""
	from ABQflow.core.runner import AbaqusRunner
	env = {'__file__': '../escape.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_absolute_path(dummy_logger, temp_output_dir):
	"""Envelope with absolute path → None."""
	from ABQflow.core.runner import AbaqusRunner
	env = {'__file__': 'C:/windows/system32/evil.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_missing_file(dummy_logger, temp_output_dir):
	"""Envelope pointing to nonexistent file → None."""
	from ABQflow.core.runner import AbaqusRunner
	env = {'__file__': 'no_such_file.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_empty_file(dummy_logger, temp_output_dir):
	"""Envelope pointing to 0-byte file → None."""
	from ABQflow.core.runner import AbaqusRunner
	empty = os.path.join(temp_output_dir, 'empty.csv')
	open(empty, 'w').close()
	env = {'__file__': 'empty.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_augments_metadata(dummy_logger, temp_output_dir):
	"""Missing columns/shape are filled from CSV; shape mismatch → overwrite."""
	from ABQflow.core.runner import AbaqusRunner
	csv_path = os.path.join(temp_output_dir, 'data.csv')
	with open(csv_path, 'w', newline='') as f:
		f.write("x,y,z\n1.0,2.0,3.0\n4.0,5.0,6.0\n7.0,8.0,9.0\n")

	# Envelope with no columns, wrong shape
	env = {'__file__': 'data.csv', 'format': 'csv', 'shape': [999, 3]}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is not None
	assert result['columns'] == ['x', 'y', 'z']
	assert result['shape'] == [3, 3]  # corrected from 999


def test_validate_envelope_non_sidecar_passthrough(dummy_logger, temp_output_dir):
	"""Non-sidecar values pass through unchanged."""
	from ABQflow.core.runner import AbaqusRunner
	assert AbaqusRunner._validate_envelope({'x': 1}, temp_output_dir, dummy_logger) == {'x': 1}
	assert AbaqusRunner._validate_envelope([1, 2], temp_output_dir, dummy_logger) == [1, 2]


# ============================================================
# 12. Log-path collision fix
# ============================================================

def test_exec_log_path_distinct_from_native_log_path(tmp_path):
	"""ABQflow's own log path never collides with Abaqus's native job log."""
	ctx = JobContext(job_name='j1', output_dir=str(tmp_path), cpus=1)
	assert ctx.exec_log_path != ctx.log_path
	assert ctx.exec_log_path == os.path.join(str(tmp_path), 'j1_abqflow.log')
	assert ctx.log_path == os.path.join(str(tmp_path), 'j1.log')


def test_abaqus_calculation_logs_to_exec_log_path_not_native_log(tmp_path):
	"""AbaqusCalculation's FileHandler writes exec_log_path, never log_path."""
	out_dir = str(tmp_path / 'job_log')
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])
	calc = AbaqusCalculation('job_log', out_dir, wf, cpus_per_job=1)
	calc.execute(phase='prepare')
	assert os.path.isfile(calc.ctx.exec_log_path)
	assert not os.path.isfile(calc.ctx.log_path)  # Abaqus never ran, native log absent


# ============================================================
# 13. Status machine: dead-state fix (PREPARING/SIMULATING/EXTRACTING/EXTRACTION_SUCCESS)
# ============================================================

def test_status_manager_current_status_live_transitions():
	mgr = JobStatusManager()
	assert mgr.current_status == JobStatus.CREATED

	mgr.mark_preparing()
	assert mgr.current_status == JobStatus.PREPARING
	mgr.record_preparation(success=True)
	assert mgr.current_status == JobStatus.PREPARATION_SUCCESS

	mgr.mark_simulating()
	assert mgr.current_status == JobStatus.SIMULATING
	mgr.record_simulation(success=True)
	assert mgr.current_status == JobStatus.SIMULATION_SUCCESS

	mgr.mark_extracting('post_extraction')
	assert mgr.current_status == JobStatus.EXTRACTING
	mgr.record_extraction({'a': 1.0})
	assert mgr.current_status == JobStatus.EXTRACTION_SUCCESS  # previously dead: never set

	assert mgr.get_final_status() == JobStatus.COMPLETED  # unaffected by the enrichment

	history = mgr.phase_history
	assert [p['phase'] for p in history] == ['preparation', 'simulation', 'post_extraction']
	for p in history:
		assert p['started_at'] is not None and p['ended_at'] is not None
		assert p['duration_s'] is not None and p['duration_s'] >= 0


def test_status_manager_extraction_failure_is_terminal():
	mgr = JobStatusManager()
	mgr.mark_extracting('post_extraction')
	mgr.record_extraction({'a': None})
	assert mgr.get_final_status() == JobStatus.EXTRACTION_FAILED
	assert mgr.current_status == JobStatus.EXTRACTION_FAILED


def test_status_manager_terminal_lock_blocks_later_marks_and_phases():
	"""First-failure-wins: no further phase transitions/records after a terminal failure."""
	mgr = JobStatusManager()
	mgr.mark_preparing()
	mgr.record_preparation(success=False, error='boom')
	assert mgr.get_final_status() == JobStatus.PREPARATION_FAILED
	n_before = len(mgr.phase_history)

	mgr.mark_simulating()
	mgr.record_simulation(success=True)
	assert mgr.get_final_status() == JobStatus.PREPARATION_FAILED
	assert mgr.current_status == JobStatus.PREPARATION_FAILED
	assert len(mgr.phase_history) == n_before  # no new phase record opened after terminal failure


def test_status_manager_compile_failure_is_terminal():
	mgr = JobStatusManager()
	mgr.mark_compiling()
	mgr.record_compile(success=False, error='syntax error in umat.for')
	assert JobStatus.SUBROUTINE_COMPILE_FAILED in _TERMINAL_FAILURES
	assert mgr.get_final_status() == JobStatus.SUBROUTINE_COMPILE_FAILED
	assert mgr.error_message == 'syntax error in umat.for'


# ============================================================
# 14. Phase separation: ModularWorkflowStrategy.prepare_only/simulate_only/extract_only
# ============================================================

class _FakePrep(PreparationStrategy):
	"""Minimal PreparationStrategy — writes a trivial INP, no subprocess calls."""

	def __init__(self, should_succeed=True):
		self.should_succeed = should_succeed

	def prepare(self, ctx, runner, logger):
		if not self.should_succeed:
			return False
		with open(ctx.inp_path, 'w') as f:
			f.write('*STEP\n*END STEP\n')
		return True


def test_modular_workflow_prepare_simulate_extract_only_compose_like_execute(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job1')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job1', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])

	results, sm = wf.prepare_only(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert os.path.isfile(ctx.inp_path)
	assert [p['phase'] for p in results['_phase_history']] == ['preparation']

	sim_results, sm, stop = wf.simulate_only(ctx, runner, dummy_logger, status_manager=sm)
	assert stop is False
	assert sim_results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in sim_results['_phase_history']] == ['preparation', 'simulation']

	ext_results, sm = wf.extract_only(ctx, runner, dummy_logger, status_manager=sm)
	assert ext_results['status'] == JobStatus.COMPLETED  # no post_extraction hooks configured


def test_modular_workflow_simulate_only_missing_inp_stops_pipeline(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job2')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job2', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])

	results, sm, stop = wf.simulate_only(ctx, runner, dummy_logger)
	assert stop is True
	assert results['status'] == JobStatus.SIMULATION_FAILED


def test_modular_workflow_extract_only_missing_odb_fails(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job3')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job3', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	post = [OdbExtractionStrategy([HookSpec('dummy_script.py', [{'result_name': 'mass'}])])]
	wf = ModularWorkflowStrategy(_FakePrep(), [], post)

	results, sm = wf.extract_only(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.EXTRACTION_FAILED
	assert results['mass'] is None


def test_modular_workflow_execute_preflight_only_stops_before_solver(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job5')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job5', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [], preflight_mode='syntaxcheck', preflight_only=True)

	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in results['_phase_history']] == ['preparation', 'preflight']
	assert not os.path.exists(ctx.odb_path)  # solver never ran


def test_abaqus_calculation_execute_phase_dispatch(tmp_path):
	out_dir = str(tmp_path / 'jobX')
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])
	calc = AbaqusCalculation('jobX', out_dir, wf, cpus_per_job=1)
	results = calc.execute(phase='prepare')
	assert results['status'] == JobStatus.COMPLETED
	assert os.path.isfile(calc.ctx.inp_path)


def test_abaqus_calculation_execute_phase_not_implemented_for_monolithic(tmp_path):
	out_dir = str(tmp_path / 'jobY')
	wf = MonolithicWorkflowStrategy('nonexistent_script.py', {})
	calc = AbaqusCalculation('jobY', out_dir, wf, cpus_per_job=1)
	with pytest.raises(NotImplementedError):
		calc.execute(phase='prepare')


def test_batch_processor_build_calc_deterministic_path_and_subroutine(tmp_path):
	spec = JobSpec('j1', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'),
					subroutine=SubroutineSpec('umat.for'))
	bp = BatchAbaqusProcessor([spec], str(tmp_path), cpus_per_job=1)
	calc = bp._build_calc(spec)
	assert calc.ctx.user_subroutine == 'umat.for'
	assert calc.ctx.output_dir == os.path.join(str(tmp_path), 'j1')
	assert isinstance(calc.workflow_strategy.compile_strategy, SubroutineCompileStrategy)


def test_batch_processor_log_job_summary_writes_phase_lines(tmp_path):
	"""The batch log now captures simulation/extraction activity via phase summaries."""
	spec = JobSpec('j1', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'))
	bp = BatchAbaqusProcessor([spec], str(tmp_path), cpus_per_job=1)
	oc = JobOutcome('j1', 'COMPLETED', duration_s=1.23,
					phases=[{'phase': 'preparation', 'status': 'PREPARATION_SUCCESS',
							'duration_s': 0.5, 'error': None},
							{'phase': 'simulation', 'status': 'SIMULATION_SUCCESS',
							'duration_s': 0.7, 'error': None}])
	bp._log_job_summary(oc)
	for h in bp.logger.handlers:
		h.flush()

	with open(os.path.join(str(tmp_path), 'batch_processor.log')) as f:
		content = f.read()
	assert 'j1: COMPLETED' in content
	assert '[preparation] PREPARATION_SUCCESS' in content
	assert '[simulation] SIMULATION_SUCCESS' in content


def test_job_outcome_pickle_roundtrip_with_phases():
	"""JobOutcome.phases must survive ProcessPoolExecutor's pickling."""
	import pickle
	oc = JobOutcome('j', 'COMPLETED', results={'x': 1.0},
					phases=[{'phase': 'preparation', 'status': 'PREPARATION_SUCCESS',
							'started_at': 1.0, 'ended_at': 2.0, 'duration_s': 1.0, 'error': None}],
					duration_s=5.0)
	restored = pickle.loads(pickle.dumps(oc))
	assert restored == oc


# ============================================================
# 15. Subroutine support
# ============================================================

def test_subroutine_spec_defaults_and_validation():
	s = SubroutineSpec('umat.for')
	assert s.language == 'fortran' and s.solver == 'standard' and s.precompiled is False

	with pytest.raises(ValueError, match='language'):
		SubroutineSpec('umat.for', language='pascal')

	with pytest.raises(ValueError, match='solver'):
		SubroutineSpec('umat.for', solver='quantum')


def test_jobspec_with_subroutine():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'),
					subroutine=SubroutineSpec('vumat.for', solver='explicit'))
	assert spec.subroutine.solver == 'explicit'


def test_build_solver_command_without_subroutine_is_unchanged(tmp_path):
	"""Regression guard: user_subroutine=None must not alter the command line."""
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=4, abaqus_exe='abaqus')
	cmd = AbaqusRunner.build_solver_command(ctx)
	assert cmd == ['abaqus', 'job=j', f'input={ctx.inp_path}', 'cpus=4', 'interactive']


def test_build_solver_command_with_subroutine(tmp_path):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=4, abaqus_exe='abaqus',
					user_subroutine='umat.for')
	cmd = AbaqusRunner.build_solver_command(ctx)
	assert cmd == ['abaqus', 'job=j', f'input={ctx.inp_path}', 'cpus=4', 'user=umat.for', 'interactive']


def test_build_preflight_command_with_subroutine(tmp_path):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=4, abaqus_exe='abaqus',
					user_subroutine='umat.for')
	cmd, chk_name = AbaqusRunner.build_preflight_command(ctx, 'datacheck')
	assert chk_name == 'j_chk'
	assert 'user=umat.for' in cmd
	assert cmd[-1] == 'cpus=1'  # datacheck's cpus=1 still appended last


def test_build_make_command_standard_vs_explicit(tmp_path):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=4, abaqus_exe='abaqus')
	cmd_std = AbaqusRunner.build_make_command(ctx, SubroutineSpec('umat.for', solver='standard'))
	cmd_exp = AbaqusRunner.build_make_command(ctx, SubroutineSpec('vumat.for', solver='explicit'))
	assert cmd_std == ['abaqus', 'make', 'library=umat.for']
	assert cmd_exp == ['abaqus', 'make', 'library=vumat.for', 'explicit']


def test_build_workflow_wires_subroutine_into_compile_strategy():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'),
					subroutine=SubroutineSpec('umat.for'))
	wf = build_workflow(spec)
	assert isinstance(wf.compile_strategy, SubroutineCompileStrategy)
	assert wf.compile_strategy.subroutine.source_path == 'umat.for'


def test_build_workflow_without_subroutine_compile_strategy_is_none():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'))
	wf = build_workflow(spec)
	assert wf.compile_strategy is None


def test_subroutine_compile_strategy_precompiled_skips_compile(tmp_path, dummy_logger):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	strat = SubroutineCompileStrategy(SubroutineSpec('umat.for', precompiled=True))
	ok, msg = strat.compile(ctx, runner, dummy_logger)
	assert ok is True and msg == ''
	assert runner.command_log == []  # never attempted to compile


def test_subroutine_compile_strategy_runs_make_via_record_only(tmp_path, dummy_logger):
	src = tmp_path / 'umat.for'
	src.write_text('C dummy umat source')
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	strat = SubroutineCompileStrategy(SubroutineSpec(str(src)), cache=False)

	ok, msg = strat.compile(ctx, runner, dummy_logger)
	assert ok is True
	assert runner.command_log[-1].stage == 'compile'


def test_modular_workflow_execute_runs_compile_before_preparation(tmp_path, dummy_logger):
	src = tmp_path / 'umat.for'
	src.write_text('C dummy')
	out_dir = str(tmp_path / 'job4')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job4', output_dir=out_dir, cpus=1, user_subroutine=str(src))
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	compile_strat = SubroutineCompileStrategy(SubroutineSpec(str(src)), cache=False)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [], compile_strategy=compile_strat)

	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in results['_phase_history']] == ['compile', 'preparation', 'simulation']


def test_subroutine_compile_strategy_failure_stops_pipeline(tmp_path, dummy_logger):
	"""A failing compile step must short-circuit before preparation runs."""
	out_dir = str(tmp_path / 'job6')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job6', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=False)  # real run_compile -> will fail (no real abaqus/source)

	class _FailingCompile:
		def compile(self, ctx, runner, logger):
			return False, "compiler error: undefined symbol"

	wf = ModularWorkflowStrategy(_FakePrep(), [], [], compile_strategy=_FailingCompile())
	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.SUBROUTINE_COMPILE_FAILED
	assert not os.path.isfile(ctx.inp_path)  # preparation never ran


def test_subroutine_needs_recompile_cache_sidecar(tmp_path, dummy_logger):
	src = tmp_path / 'umat.for'
	src.write_text('C v1')
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger)
	sub = SubroutineSpec(str(src))

	assert runner.subroutine_needs_recompile(sub) is True  # no prior compile record
	runner._record_compile_hash(sub)
	assert runner.subroutine_needs_recompile(sub) is False  # unchanged content

	src.write_text('C v2 changed content')
	assert runner.subroutine_needs_recompile(sub) is True  # content changed -> recompile
