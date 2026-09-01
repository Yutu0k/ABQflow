"""Shared fixtures for the whole test suite, plus the opt-in marker gates.

Two kinds of test are skipped by default because they need something the
machine running pytest may not have:

``@pytest.mark.abaqus``
	Launches the real Abaqus executable — slow, and consumes license tokens.
	Enabled with ``--run-abaqus``.
``@pytest.mark.remote``
	Connects to a real remote machine over SSH.  Enabled with
	``--run-remote``, and additionally needs ``ABQFLOW_REMOTE_*`` environment
	variables to say which machine.

An end-to-end remote solve carries both markers and therefore needs both
flags.
"""

import logging
import os
import shutil
import tempfile

import pytest


def pytest_addoption(parser):
	parser.addoption(
		"--run-abaqus", action="store_true", default=False,
		help="Run tests marked 'abaqus' that launch the real Abaqus solver "
			"(slow, consumes license tokens). Skipped by default.",
	)
	parser.addoption(
		"--run-remote", action="store_true", default=False,
		help="Run tests marked 'remote' that connect to a real SSH host "
			"(needs ABQFLOW_REMOTE_* env vars). Skipped by default.",
	)


def pytest_configure(config):
	config.addinivalue_line(
		"markers",
		"abaqus: test launches the real Abaqus executable (opt-in via --run-abaqus)",
	)
	config.addinivalue_line(
		"markers",
		"remote: test connects to a real remote machine (opt-in via --run-remote)",
	)


def pytest_collection_modifyitems(config, items):
	gates = [
		("abaqus", "--run-abaqus", "tests that launch real Abaqus"),
		("remote", "--run-remote", "tests that connect to a real remote machine"),
	]
	for marker, flag, what in gates:
		if config.getoption(flag):
			continue
		skip = pytest.mark.skip(reason=f"needs {flag} to run {what}")
		for item in items:
			if marker in item.keywords:
				item.add_marker(skip)


def _resolve_abaqus_exe():
	"""Locate a real Abaqus executable: env override, common Windows install, or PATH."""
	env = os.environ.get("ABQFLOW_ABAQUS_EXE")
	if env and os.path.isfile(env):
		return env
	default = "C:/Applications/SIMULIA/Commands/2026/abaqus.bat"
	if os.path.isfile(default):
		return default
	return shutil.which("abaqus") or shutil.which("abaqus.bat")


@pytest.fixture(scope="session")
def abaqus_exe():
	"""Absolute path to a real Abaqus executable; skips the test if none is found."""
	exe = _resolve_abaqus_exe()
	if not exe:
		pytest.skip("No Abaqus executable found (set ABQFLOW_ABAQUS_EXE to override)")
	return exe


@pytest.fixture(scope="session")
def remote_host():
	"""A :class:`HostSpec` built from ``ABQFLOW_REMOTE_*``; skips if unset.

	Recognised variables (``HOST``/``HOSTNAME`` and ``USER``/``USERNAME`` are
	interchangeable)::

		ABQFLOW_REMOTE_HOST         hostname or bare IPv6/IPv4 literal
		ABQFLOW_REMOTE_USER         account name
		ABQFLOW_REMOTE_PASSWORD     account password
		ABQFLOW_REMOTE_ABAQUS_EXE   absolute path to abaqus.bat on that machine
		ABQFLOW_REMOTE_WORK_ROOT    remote directory for per-job folders
	"""
	from ABQflow.core.hosts import HostSpec

	def _env(*names, default=None):
		for name in names:
			value = os.environ.get("ABQFLOW_REMOTE_" + name)
			if value:
				return value
		return default

	hostname = _env("HOST", "HOSTNAME")
	if not hostname:
		pytest.skip("set ABQFLOW_REMOTE_HOST (and USER/PASSWORD) to run remote tests")

	return HostSpec(
		name=_env("NAME", default="itest"),
		hostname=hostname,
		username=_env("USER", "USERNAME"),
		password=_env("PASSWORD"),
		port=int(_env("PORT", default="22")),
		abaqus_exe=_env("ABAQUS_EXE", default="abaqus"),
		work_root=_env("WORK_ROOT", default=r"D:\abqwork"),
		cpus_total=int(_env("CPUS_TOTAL", default="8")),
	)


@pytest.fixture
def dummy_logger():
	return logging.getLogger('test_validate')


@pytest.fixture
def temp_output_dir():
	with tempfile.TemporaryDirectory() as d:
		yield d
