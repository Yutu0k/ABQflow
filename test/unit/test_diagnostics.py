"""Tests for ABQflow.core.diagnostics — truth table, .sta parsing, error harvesting.

Run: pytest test/unit/test_diagnostics.py -v
"""

import glob
import os
import tempfile

import pytest

from ABQflow import (
	SolverDiagnostics,
	SolverResult,
	apply_truth_table,
	diagnose,
	harvest_errors,
	parse_sta,
)


@pytest.fixture
def real_sta_files():
	"""Real .sta files committed under examples/ (pre-computed, COMPLETED Standard jobs)."""
	files = sorted(glob.glob('examples/**/*.sta', recursive=True))
	if not files:
		pytest.skip("No real .sta files found under examples/")
	return files


# ============================================================
# Truth table (IMP-02)
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
# STA parsing
# ============================================================

def test_parse_sta_real_file(real_sta_files):
	"""Real .sta files from examples/ parse correctly."""
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
# Error harvesting
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
# diagnose
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
# Dataclass defaults
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
