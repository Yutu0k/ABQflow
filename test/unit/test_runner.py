"""Tests for ABQflow.core.runner — envelope validation and pure command-builders.

Run: pytest test/unit/test_runner.py -v
"""

import filecmp
import os
import sys

import pytest

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


# ============================================================
# Interpreter selection: 'abaqus' vs 'host'
# ============================================================

@pytest.mark.parametrize('needs_cae_kernel, has_abqpy, expected', [
	(False, False, ['abaqus', 'python', 's.py']),
	(True, False, ['abaqus', 'cae', 'noGUI=s.py', '--']),
	(False, True, ['python', 's.py']),
	(True, True, ['python', 's.py']),
])
def test_build_script_command_default_interpreter_unchanged(
		needs_cae_kernel, has_abqpy, expected):
	"""Regression guard: adding `interpreter` must not move the old branches."""
	assert AbaqusRunner.build_script_command(
		's.py', needs_cae_kernel, 'abaqus', has_abqpy) == expected


@pytest.mark.parametrize('needs_cae_kernel, has_abqpy', [
	(False, False), (True, False), (False, True), (True, True),
])
def test_build_script_command_host_outranks_everything(needs_cae_kernel, has_abqpy):
	"""'host' describes the artifact, not the environment: a .dat is text, so
	neither abqpy nor the CAE kernel has any say."""
	cmd = AbaqusRunner.build_script_command(
		's.py', needs_cae_kernel, 'abaqus', has_abqpy, interpreter='host')
	assert cmd == [sys.executable, 's.py']


def test_build_script_command_host_honours_env_override(monkeypatch):
	monkeypatch.setenv('ABQFLOW_HOST_PYTHON', '/opt/py/bin/python')
	cmd = AbaqusRunner.build_script_command('s.py', False, 'abaqus', False,
											interpreter='host')
	assert cmd == ['/opt/py/bin/python', 's.py']


def test_build_script_command_rejects_unknown_interpreter():
	with pytest.raises(ValueError, match='interpreter must be one of'):
		AbaqusRunner.build_script_command('s.py', False, 'abaqus', False,
											interpreter='pypy')


# ============================================================
# Staging the single-file modules hooks import
# ============================================================

def test_stage_hookkit_stages_extra_modules(tmp_path, dummy_logger):
	src_dir = os.path.join(
		os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
		'src', 'ABQflow')
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger)

	runner._stage_hookkit()
	assert os.path.isfile(tmp_path / 'hookkit.py')
	assert not os.path.exists(tmp_path / 'datkit.py')   # staged on demand only

	runner._stage_hookkit(extra_modules=('datkit.py',))
	for name in ('hookkit.py', 'datkit.py'):
		assert filecmp.cmp(str(tmp_path / name), os.path.join(src_dir, name),
							shallow=False)


def test_stage_hookkit_is_idempotent(tmp_path, dummy_logger):
	"""Identical content is not re-copied — re-running a job must not churn."""
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger)

	runner._stage_hookkit(extra_modules=('datkit.py',))
	stamps = {name: (tmp_path / name).stat().st_mtime_ns
				for name in ('hookkit.py', 'datkit.py')}
	runner._stage_hookkit(extra_modules=('datkit.py',))
	for name, stamp in stamps.items():
		assert (tmp_path / name).stat().st_mtime_ns == stamp


def test_stage_hookkit_warns_but_does_not_raise_on_a_missing_module(
		tmp_path, dummy_logger):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger)
	runner._stage_hookkit(extra_modules=('no_such_module.py',))
	assert os.path.isfile(tmp_path / 'hookkit.py')


# ============================================================
# A host hook on a remote backend runs here, not there
# ============================================================

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DAT_HOOK = os.path.join(_REPO, 'examples', 'extraction_scripts', 'get_dat_results.py')
_DAT_FIXTURE = os.path.join(_REPO, 'test', 'fixtures', 'dat', 'el_print_mixed.dat')


def test_run_hook_host_executes_locally_and_never_uploads(tmp_path, dummy_logger):
	"""With a remote backend and ``interpreter='host'``, the hook must run on
	this machine against the local ``.dat``: nothing is shipped, no path is
	remapped, and the sidecar CSV stays where ``_validate_envelope`` looks."""
	import shutil

	from ABQflow import RecordingBackend

	job_dir = tmp_path / 'job001'
	job_dir.mkdir()
	remote_root = tmp_path / 'remote'
	backend = RecordingBackend(work_root=str(remote_root))

	ctx = JobContext(job_name='job001', output_dir=str(job_dir), cpus=1)
	shutil.copy2(_DAT_FIXTURE, ctx.dat_path)
	runner = AbaqusRunner(ctx, dummy_logger, backend=backend)
	assert runner.is_remote

	results = runner.run_hook(
		script_path=_DAT_HOOK,
		tasks=[{'result_name': 'max_stress_mises'},
				{'result_name': 'mises_field', 'output': 'file'}],
		common_args={'--dat_path': ctx.dat_path},
		needs_cae_kernel=False,
		interpreter='host',
		extra_modules=('datkit.py',))

	# The hook really ran — these are parsed values, not the backend's script.
	assert results['max_stress_mises'] == pytest.approx(4473.0)
	assert results['mises_field']['__file__'] == 'job001_mises_field.csv'
	assert results['mises_field']['columns'] == ['ELEMENT', 'PT', 'MISES']
	assert results['mises_field']['shape'] == [13, 3]
	assert os.path.isfile(job_dir / 'job001_mises_field.csv')

	# ...and the remote machine was never touched.
	assert backend.command_log == []
	assert backend.files == {}
	assert backend.fetched == []
	assert os.path.isfile(job_dir / 'datkit.py')


def test_run_hook_host_record_only_shows_the_local_command(tmp_path, dummy_logger):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)

	results = runner.run_hook(
		script_path=_DAT_HOOK, tasks=[{'result_name': 'x'}],
		common_args={'--dat_path': ctx.dat_path},
		needs_cae_kernel=False, interpreter='host')

	assert results == {'x': None}
	cmd = runner.command_log[-1].cmd
	assert cmd[0] == sys.executable
	assert '--dat_path' in cmd


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
