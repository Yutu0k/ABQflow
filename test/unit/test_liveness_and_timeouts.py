"""Completion detection by rc sentinel *and* process liveness, plus per-stage timeouts.

Both exist for the same reason: jobs that legitimately run for hours.

The rc sentinel alone cannot tell "still solving" from "killed without
finishing", so a job that died minutes in would be discovered only when the
wall-clock budget expired — the wrong trade when that budget is measured in
hours.  And a single ``timeout`` number used to govern the solver wait,
preflight, compilation and every hook at once, so raising it for a long solve
also handed a hung hook script the same many-hour budget.

Run: pytest test/unit/test_liveness_and_timeouts.py -v
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys

import pytest

from ABQflow.core.backends import LocalBackend, RecordingBackend
from ABQflow.core.backends.base import JobHandle
from ABQflow.core.context import JobContext
from ABQflow.core.runner import AbaqusRunner, Timeouts


@pytest.fixture
def logger():
	log = logging.getLogger('test_liveness')
	log.addHandler(logging.NullHandler())
	return log


@pytest.fixture
def ctx(tmp_path):
	job_dir = tmp_path / 'local' / 'j1'
	job_dir.mkdir(parents=True)
	return JobContext(job_name='j1', output_dir=str(job_dir), cpus=2,
					abaqus_exe='abaqus')


# ============================================================
# liveness: the decision table
# ============================================================

def test_rc_present_means_finished(tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path), poll_sequence=[0])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	assert backend.wait(handle, settle_s=0)[:2] == ('finished', 0)


def test_process_gone_without_a_sentinel_is_reported_as_died(tmp_path):
	"""The gap the sentinel alone cannot cover.

	Before liveness was consulted this case waited out the entire timeout —
	hours, for a job that had already stopped existing.
	"""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None],
							alive_sequence=[False])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, rc, _ = backend.wait(handle, timeout_s=3600, settle_s=0)
	assert verdict == 'died'
	assert rc is None


def test_died_is_reached_quickly_not_after_the_timeout(tmp_path):
	"""A death must not cost the whole budget to notice."""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None],
							alive_sequence=[False])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, _rc, elapsed = backend.wait(handle, timeout_s=10_000, settle_s=0)
	assert verdict == 'died'
	assert elapsed < 5


def test_a_sentinel_appearing_during_the_settle_window_wins(tmp_path):
	"""The race: the launcher exits between the rc read and the liveness check.

	Its final act is to write the sentinel, so "process gone" must be
	confirmed by re-reading rather than believed immediately — otherwise a
	perfectly successful job is reported as having died.
	"""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None, 0],
							alive_sequence=[False])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, rc, _ = backend.wait(handle, timeout_s=3600, settle_s=0)
	assert (verdict, rc) == ('finished', 0)


def test_unknown_liveness_never_counts_as_death(tmp_path):
	"""``None`` means "cannot tell", and must not end a healthy solve.

	A transient query failure is not evidence that a four-hour job has died.
	"""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None, None, 0],
							alive_sequence=[None])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, rc, _ = backend.wait(handle, timeout_s=3600, interval=0,
								settle_s=0)
	assert (verdict, rc) == ('finished', 0)


def test_a_live_process_outlives_a_none_timeout(tmp_path):
	"""With no wall-clock cap, only the process decides when it is over."""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None, None, None, 0],
							alive_sequence=[True])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, rc, _ = backend.wait(handle, timeout_s=None, interval=0,
								settle_s=0)
	assert (verdict, rc) == ('finished', 0)


def test_timeout_still_applies_to_a_live_process(tmp_path):
	"""Liveness removes the need for a deadline; it does not remove the option."""
	backend = RecordingBackend(work_root=str(tmp_path),
							poll_sequence=[None],
							alive_sequence=[True])
	handle = backend.submit_detached(['abaqus'], str(tmp_path), 'j')
	verdict, rc, _ = backend.wait(handle, timeout_s=0, settle_s=0)
	assert (verdict, rc) == ('timeout', None)


# ============================================================
# liveness: backend implementations
# ============================================================

def test_local_backend_reports_a_running_process_as_alive(tmp_path):
	backend = LocalBackend()
	handle = backend.submit_detached(
		[sys.executable, '-c', 'import time; time.sleep(30)'], str(tmp_path), 'j')
	try:
		assert backend.is_alive(handle) is True
	finally:
		backend.terminate(handle, 'abaqus', grace_s=1)
		backend.close()


def test_local_backend_reports_an_exited_process_as_dead(tmp_path):
	backend = LocalBackend()
	handle = backend.submit_detached(
		[sys.executable, '-c', 'pass'], str(tmp_path), 'j')
	backend.wait(handle, timeout_s=60)
	assert backend.is_alive(handle) is False
	backend.close()


def test_local_backend_cannot_tell_without_a_process_handle(tmp_path):
	"""A resumed session owns no Popen, and says so rather than guessing."""
	backend = LocalBackend()
	handle = JobHandle('never_started', str(tmp_path), pid=1234)
	assert backend.is_alive(handle) is None
	backend.close()


def test_ssh_backend_cannot_tell_without_a_pid():
	"""No PID from the launcher means liveness is unanswerable, not negative."""
	from ABQflow.core.backends.ssh import SshBackend
	from ABQflow.core.hosts import HostSpec

	host = HostSpec(name='h', hostname='h.example', work_root=r'D:\w',
					abaqus_exe=r'C:\abaqus.bat')
	backend = SshBackend(host)
	assert backend.is_alive(JobHandle('j', r'D:\w\j', pid=None)) is None


# ============================================================
# per-stage timeouts
# ============================================================

def test_scalar_timeout_keeps_the_historical_meaning():
	"""``timeout=3600`` used to mean 3600 for everything; it still does."""
	t = Timeouts.coerce(3600)
	assert (t.solver, t.preflight, t.compile, t.hook) == (3600, 3600, 3600, 3600)


def test_no_timeout_selects_per_stage_defaults():
	t = Timeouts.coerce(None)
	assert t.solver is None, "a long solve is worth waiting for"
	assert t.preflight and t.compile and t.hook, "a hung short stage is not"


def test_coerce_passes_a_timeouts_through():
	given = Timeouts(solver=None, hook=60)
	assert Timeouts.coerce(given) is given


def test_coerce_accepts_a_mapping():
	t = Timeouts.coerce({'solver': None, 'hook': 42})
	assert t.solver is None and t.hook == 42


def test_unlimited_solver_does_not_leak_into_hooks():
	"""The whole point of splitting them.

	Raising the solver budget for a four-hour job must not also let a hung
	extraction hook block for four hours.
	"""
	t = Timeouts(solver=None)
	assert t.hook is not None
	assert t.preflight is not None


def test_runner_exposes_the_split_timeouts(ctx, logger):
	runner = AbaqusRunner(ctx, logger, timeout=Timeouts(solver=None, hook=30))
	assert runner.timeouts.solver is None
	assert runner.timeouts.hook == 30


def test_runner_still_accepts_a_scalar(ctx, logger):
	runner = AbaqusRunner(ctx, logger, timeout=900)
	assert runner.timeouts.solver == 900
	assert runner.timeout == 900, "the original attribute stays readable"


def test_hook_timeout_is_the_one_applied_to_commands(ctx, logger, tmp_path):
	"""A hook that overruns is cut off by ``hook``, not by ``solver``."""
	runner = AbaqusRunner(ctx, logger,
						timeout=Timeouts(solver=None, hook=0.5))
	proc = runner._run([sys.executable, '-c', 'import time; time.sleep(20)'],
					cwd=str(tmp_path), stage='hook')
	assert proc is None, "the command should have been cut off"


def test_grace_period_follows_the_solver_timeout(ctx, logger):
	assert AbaqusRunner(ctx, logger, timeout=Timeouts(solver=None))._grace_period() == 300
	assert AbaqusRunner(ctx, logger, timeout=2000)._grace_period() == 100
	assert AbaqusRunner(ctx, logger, timeout=100)._grace_period() == 30
