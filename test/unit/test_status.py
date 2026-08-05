"""Tests for ABQflow.core.status — JobStatus / JobStatusManager state machine.

Run: pytest test/unit/test_status.py -v
"""

from ABQflow import JobStatus
from ABQflow.core.status import _TERMINAL_FAILURES, JobStatusManager


# ============================================================
# Preflight (IMP-04)
# ============================================================

def test_preflight_failed_status():
	assert JobStatus.PREFLIGHT_FAILED in _TERMINAL_FAILURES

	mgr = JobStatusManager()
	mgr.record_preflight(success=False, error="Bad INP")
	assert mgr.get_final_status() == JobStatus.PREFLIGHT_FAILED
	assert mgr.error_message == "Bad INP"

	# First-failure-wins: subsequent transition is no-op
	mgr.record_simulation(success=True)
	assert mgr.get_final_status() == JobStatus.PREFLIGHT_FAILED


# ============================================================
# Status machine: dead-state fix (PREPARING/SIMULATING/EXTRACTING/EXTRACTION_SUCCESS)
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
