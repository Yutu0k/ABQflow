"""Launching a bare executable name on Windows.

``subprocess`` goes through ``CreateProcess``, which searches ``PATH`` but not
``PATHEXT`` — it only ever appends ``.exe``.  So ``Popen(['abaqus'])`` fails
with ``FileNotFoundError: [WinError 2]`` on a machine where ``abaqus.bat`` is
properly installed and on ``PATH``, and where :func:`shutil.which` finds it
without trouble.

That made the default ``abaqus_exe='abaqus'`` unusable for local execution,
and the error named no file, so it read as "something is missing" rather than
"this name cannot be launched".

Run: pytest test/unit/test_local_exe_resolution.py -v
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

from ABQflow.core.backends.local import (
	LocalBackend,
	_launch_error,
	resolve_local_exe,
)


# ============================================================
# resolution
# ============================================================

def test_absolute_path_is_returned_untouched():
	path = r'C:\SIMULIA\Commands\abaqus.bat'
	assert resolve_local_exe(path) == path


def test_relative_path_with_a_separator_is_untouched():
	assert resolve_local_exe(os.path.join('bin', 'abaqus.bat')) == \
		os.path.join('bin', 'abaqus.bat')


def test_bare_name_resolves_through_pathext():
	"""What shutil.which finds, CreateProcess must be handed explicitly."""
	resolved = resolve_local_exe('python')
	assert resolved == shutil.which('python')
	assert os.path.isabs(resolved)


def test_unresolvable_name_comes_back_as_given():
	"""So the caller can report exactly what the user configured."""
	assert resolve_local_exe('no-such-binary-xyz') == 'no-such-binary-xyz'


def test_empty_stays_empty():
	assert resolve_local_exe('') == ''


# ============================================================
# error message
# ============================================================

def test_missing_executable_error_names_the_file():
	err = _launch_error(['abaqus', 'job=x'], FileNotFoundError(2, 'not found'))
	assert "'abaqus'" in err
	assert 'absolute path' in err


def test_missing_executable_error_explains_the_pathext_trap():
	err = _launch_error(['abaqus'], FileNotFoundError(2, 'not found'))
	assert 'PATHEXT' in err


def test_other_errors_are_reported_verbatim():
	err = _launch_error(['x'], PermissionError('denied'))
	assert err == 'PermissionError: denied'


# ============================================================
# through the backend
# ============================================================

def test_run_resolves_a_bare_interpreter_name(tmp_path):
	"""A bare name that PATHEXT can resolve must actually launch."""
	backend = LocalBackend()
	res = backend.run(['python', '-c', 'print("resolved")'], str(tmp_path))
	assert res.returncode == 0
	assert 'resolved' in res.stdout


def test_run_reports_an_unresolvable_name_clearly(tmp_path):
	backend = LocalBackend()
	res = backend.run(['no-such-binary-xyz'], str(tmp_path))
	assert res.returncode is None
	assert 'no-such-binary-xyz' in res.stderr
	assert 'absolute path' in res.stderr


def test_submit_detached_reports_an_unresolvable_name_clearly(tmp_path):
	backend = LocalBackend()
	handle = backend.submit_detached(['no-such-binary-xyz'], str(tmp_path), 'j1')
	assert handle.launch_rc == 1
	assert 'no-such-binary-xyz' in handle.launch_output
	assert 'abaqus.bat' in handle.launch_output, "point the user at the fix"
	backend.close()


def test_bare_name_would_fail_without_resolution(tmp_path):
	"""Pin the underlying behaviour this module works around.

	Skipped off Windows, where CreateProcess semantics do not apply.
	"""
	if sys.platform != 'win32':
		pytest.skip('CreateProcess/PATHEXT behaviour is Windows-only')
	if not shutil.which('python'):
		pytest.skip('python not on PATH')

	# 'python' happens to be a .exe, so pick something that is not: build a
	# .bat in a directory we put on PATH, then show the bare name fails.
	bat = tmp_path / 'abqprobe.bat'
	bat.write_text('@echo off\r\necho PROBE_OK\r\n')
	env_path = os.environ['PATH']
	os.environ['PATH'] = str(tmp_path) + os.pathsep + env_path
	try:
		assert shutil.which('abqprobe'), "which() should find it via PATHEXT"
		with pytest.raises(FileNotFoundError):
			subprocess.run(['abqprobe'], capture_output=True)
		# ...while the resolved form launches.
		res = LocalBackend().run(['abqprobe'], str(tmp_path))
		assert res.returncode == 0 and 'PROBE_OK' in res.stdout
	finally:
		os.environ['PATH'] = env_path
