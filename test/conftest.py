"""Shared fixtures for the whole test suite, plus the ``abaqus`` marker gate.

Tests marked ``@pytest.mark.abaqus`` launch the real Abaqus executable
(slow, consumes license tokens) and are skipped unless ``--run-abaqus`` is
passed on the command line.
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


def pytest_configure(config):
	config.addinivalue_line(
		"markers",
		"abaqus: test launches the real Abaqus executable (opt-in via --run-abaqus)",
	)


def pytest_collection_modifyitems(config, items):
	if config.getoption("--run-abaqus"):
		return
	skip_abaqus = pytest.mark.skip(reason="needs --run-abaqus to run tests that launch real Abaqus")
	for item in items:
		if "abaqus" in item.keywords:
			item.add_marker(skip_abaqus)


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


@pytest.fixture
def dummy_logger():
	return logging.getLogger('test_validate')


@pytest.fixture
def temp_output_dir():
	with tempfile.TemporaryDirectory() as d:
		yield d
