"""Tests for ABQflow.core.runner — envelope validation and pure command-builders.

Run: pytest test/unit/test_runner.py -v
"""

import os

from ABQflow import JobContext, SubroutineSpec
from ABQflow.core.runner import AbaqusRunner


# ============================================================
# Runner envelope validation tests (HK-01 §3.6)
# ============================================================

def test_validate_envelope_path_escape(dummy_logger, temp_output_dir):
	"""Envelope with ../ path → None."""
	env = {'__file__': '../escape.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_absolute_path(dummy_logger, temp_output_dir):
	"""Envelope with absolute path → None."""
	env = {'__file__': 'C:/windows/system32/evil.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_missing_file(dummy_logger, temp_output_dir):
	"""Envelope pointing to nonexistent file → None."""
	env = {'__file__': 'no_such_file.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_empty_file(dummy_logger, temp_output_dir):
	"""Envelope pointing to 0-byte file → None."""
	empty = os.path.join(temp_output_dir, 'empty.csv')
	open(empty, 'w').close()
	env = {'__file__': 'empty.csv', 'format': 'csv'}
	result = AbaqusRunner._validate_envelope(env, temp_output_dir, dummy_logger)
	assert result is None


def test_validate_envelope_augments_metadata(dummy_logger, temp_output_dir):
	"""Missing columns/shape are filled from CSV; shape mismatch → overwrite."""
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
	assert AbaqusRunner._validate_envelope({'x': 1}, temp_output_dir, dummy_logger) == {'x': 1}
	assert AbaqusRunner._validate_envelope([1, 2], temp_output_dir, dummy_logger) == [1, 2]


# ============================================================
# Subroutine support: pure command builders
# ============================================================

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
