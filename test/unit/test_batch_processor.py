"""Tests for ABQflow.core.abaqus_automation — BatchAbaqusProcessor, JobOutcome, dry-run.

None of these launch a real Abaqus process: dry_run('plan') is a pure
computation, and preflight/build_calc/log_summary/pickling tests only touch
specs, paths, and logging.

Run: pytest test/unit/test_batch_processor.py -v
"""

import os
import pickle

import pytest

from ABQflow import (
	BatchAbaqusProcessor,
	CommandRecord,
	HostSpec,
	JobOutcome,
	JobPlan,
	JobSpec,
	PreparationSpec,
	SubroutineCompileStrategy,
	SubroutineSpec,
)
from ABQflow.core.status import JobStatus, JobStatusManager


# ============================================================
# Preflight (IMP-04): batch-level preflight_only construction
# ============================================================

def test_batch_processor_preflight_only():
	spec = JobSpec('pf', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'))
	bp = BatchAbaqusProcessor([spec], './test/test_pf', cpus_per_job=4, preflight_only=True)
	assert bp.preflight_only is True


# ============================================================
# Dry-run (IMP-05)
# ============================================================

def test_dry_run_plan():
	spec = JobSpec('dry_test',
				preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'),
				preflight='syntaxcheck')
	bp = BatchAbaqusProcessor([spec], './test/test_dry_plan', cpus_per_job=4)
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
	assert not os.path.isdir('./test/test_dry_plan/dry_test')


def test_dry_run_bad_level():
	bp = BatchAbaqusProcessor(
		[JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'))],
		'./test/test_dry', cpus_per_job=4)
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
# Phase separation: batch-level building/logging/pickling
# ============================================================

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
	oc = JobOutcome('j', 'COMPLETED', results={'x': 1.0},
					phases=[{'phase': 'preparation', 'status': 'PREPARATION_SUCCESS',
							'started_at': 1.0, 'ended_at': 2.0, 'duration_s': 1.0, 'error': None}],
					duration_s=5.0)
	restored = pickle.loads(pickle.dumps(oc))
	assert restored == oc


# ============================================================
# JobOutcome.diagnostics pass-through + serialization
# ============================================================

def test_job_outcome_diagnostics():
	oc = JobOutcome('j1', 'COMPLETED')
	assert oc.diagnostics is None

	oc2 = JobOutcome('j2', 'SIMULATION_FAILED', error='err',
					diagnostics={'sta_verdict': 'NOT_COMPLETED', 'errors': ['x']})
	assert oc2.diagnostics['sta_verdict'] == 'NOT_COMPLETED'


def test_job_outcome_carries_results_alongside_diagnostics():
	oc = JobOutcome('j1', 'FAILED', error='err',
					diagnostics={'sta_verdict': 'ABORTED'},
					results={'x': 1.0})

	assert oc.diagnostics == {'sta_verdict': 'ABORTED'}
	assert oc.results == {'x': 1.0}
	assert oc.error == 'err'


# ============================================================
# JobOutcome.error is populated on every failure path
# ============================================================

class _FakeCtx:
	def __init__(self, output_dir):
		self.output_dir = output_dir


class _FakeCalc:
	"""Stands in for AbaqusCalculation: execute() replays a canned dict."""

	def __init__(self, job_name, results, output_dir='/out', raises=None,
				host=None):
		self.job_name = job_name
		self.ctx = _FakeCtx(output_dir)
		self.host = host
		self._results = results
		self._raises = raises

	def execute(self, phase='full'):
		if self._raises is not None:
			raise self._raises
		return dict(self._results)


def test_worker_promotes_modular_error_to_the_outcome():
	"""The regression: a workflow failure left JobOutcome.error as None, so
	the reason was reachable only by digging through `phases`."""
	from ABQflow.core.abaqus_automation import _worker

	sm = JobStatusManager()
	sm.mark_compiling()
	sm.record_compile(success=False, error='driverExceptions.CompileError: umat.for')

	oc = _worker(_FakeCalc('j', sm.finalize_into({})))
	assert oc.status == 'SUBROUTINE_COMPILE_FAILED'
	assert oc.error == 'driverExceptions.CompileError: umat.for'
	assert oc.results == {}            # reserved keys popped back out
	assert oc.phases[0]['phase'] == 'compile'


def test_worker_promotes_monolithic_error_out_of_results():
	"""Monolithic workflows set results['error'] directly. It used to stay
	misfiled there — and they record no phases, so nothing surfaced it."""
	from ABQflow.core.abaqus_automation import _worker

	oc = _worker(_FakeCalc('j', {'status': JobStatus.JSON_DECODE_ERROR,
								'error': 'Expecting value: line 1 column 1'}))
	assert oc.status == 'JSON_DECODE_ERROR'
	assert oc.error == 'Expecting value: line 1 column 1'
	assert oc.phases is None
	assert 'error' not in (oc.results or {})


def test_worker_keeps_error_none_on_success():
	from ABQflow.core.abaqus_automation import _worker

	sm = JobStatusManager()
	sm.mark_preparing()
	sm.record_preparation(success=True)

	oc = _worker(_FakeCalc('j', sm.finalize_into({'mass': 1.0})))
	assert oc.status == 'COMPLETED'
	assert oc.error is None
	assert oc.results == {'mass': 1.0}


def test_worker_error_carries_a_traceback_when_a_bug_escapes():
	from ABQflow.core.abaqus_automation import _worker

	oc = _worker(_FakeCalc('j', None, raises=ValueError('boom')))
	assert oc.status == 'UNKNOWN_ERROR'
	assert 'ValueError: boom' in oc.error
	assert 'Traceback' in oc.error      # the escaping frame is the whole point
	assert oc.output_dir == '/out'


def test_extraction_error_names_the_failing_tasks():
	sm = JobStatusManager()
	sm.mark_extracting('post_extraction')
	sm.record_extraction({'mass': 1.0, 'stress': None, 'disp': None})

	assert sm.error_message == 'Extraction task(s) returned None: disp, stress'


def test_finalize_into_omits_error_on_success():
	sm = JobStatusManager()
	sm.mark_preparing()
	sm.record_preparation(success=True)

	assert 'error' not in sm.finalize_into({})


# ============================================================
# JobOutcome.diagnostics presence — the truth table documented in
# docs/source/getting_started/quick_start.rst. Diagnostics are harvested
# after every solver run but only *attached* when the run was not clean,
# so a healthy batch carries None throughout.
# ============================================================

def _outcome_for(solver_result, sim_ok):
	"""Drive the exact attach rule from ModularWorkflowStrategy.simulate_only."""
	from dataclasses import asdict

	from ABQflow.core.abaqus_automation import _worker

	results = {}
	if solver_result.diagnostics is not None:
		if not solver_result.success or solver_result.error:
			results['diagnostics'] = asdict(solver_result.diagnostics)
	sm = JobStatusManager()
	sm.mark_simulating()
	sm.record_simulation(success=sim_ok,
						error=None if sim_ok else solver_result.error)
	sm.finalize_into(results)
	return _worker(_FakeCalc('j', results))


def test_diagnostics_is_none_on_a_clean_solver_run():
	"""What examples 01/07/08 actually show: a batch that worked reports
	diagnostics=None on every job."""
	from ABQflow.core.diagnostics import SolverDiagnostics, SolverResult

	oc = _outcome_for(SolverResult(success=True, error=None,
								diagnostics=SolverDiagnostics(sta_verdict='COMPLETED')), True)
	assert oc.status == 'COMPLETED'
	assert oc.diagnostics is None


def test_diagnostics_is_populated_when_the_solver_fails():
	from ABQflow.core.diagnostics import SolverDiagnostics, SolverResult

	oc = _outcome_for(SolverResult(
		success=False, error='ERROR: too many increments',
		diagnostics=SolverDiagnostics(sta_verdict='NOT_COMPLETED',
									errors=['ERROR: too many increments'],
									error_total=1)), False)
	assert oc.status == 'SIMULATION_FAILED'
	assert oc.diagnostics['sta_verdict'] == 'NOT_COMPLETED'
	assert oc.error == 'ERROR: too many increments'


def test_diagnostics_on_a_completed_job_is_the_rc_mismatch_signal():
	"""rc!=0 with .sta COMPLETED: status stays COMPLETED and error stays
	None, so the *presence* of diagnostics is the only warning."""
	from ABQflow.core.diagnostics import SolverDiagnostics, SolverResult

	oc = _outcome_for(SolverResult(success=True, error='rc=1 but analysis completed',
								diagnostics=SolverDiagnostics(sta_verdict='COMPLETED')), True)
	assert oc.status == 'COMPLETED'
	assert oc.error is None
	assert oc.diagnostics is not None


def test_diagnostics_is_none_when_the_job_never_reached_the_solver():
	from ABQflow.core.abaqus_automation import _worker

	sm = JobStatusManager()
	sm.mark_compiling()
	sm.record_compile(success=False, error='CompileError')
	oc = _worker(_FakeCalc('j', sm.finalize_into({})))
	assert oc.status == 'SUBROUTINE_COMPILE_FAILED'
	assert oc.diagnostics is None


def test_simulation_failed_can_still_carry_no_diagnostics():
	"""A simulate-only run with no INP fails before launching the solver."""
	from ABQflow.core.abaqus_automation import _worker

	sm = JobStatusManager()
	sm.mark_simulating()
	sm.record_simulation(success=False, error='INP not found: x.inp')
	oc = _worker(_FakeCalc('j', sm.finalize_into({})))
	assert oc.status == 'SIMULATION_FAILED'
	assert oc.diagnostics is None      # so guard it, don't index it


def test_diagnostics_is_empty_not_absent_when_staging_fails():
	from ABQflow.core.diagnostics import SolverDiagnostics, SolverResult

	oc = _outcome_for(SolverResult(success=False, error='Failed to stage input files',
								diagnostics=SolverDiagnostics()), False)
	assert oc.diagnostics['sta_verdict'] == 'INDETERMINATE'
	assert oc.diagnostics['errors'] == []
	assert oc.error == 'Failed to stage input files'


def test_monolithic_outcome_never_carries_diagnostics():
	from ABQflow.core.abaqus_automation import _worker

	oc = _worker(_FakeCalc('j', {'status': JobStatus.JSON_DECODE_ERROR,
								'error': 'bad json'}))
	assert oc.diagnostics is None
	assert oc.phases is None


def test_log_job_summary_still_reports_error_when_phases_exist(tmp_path):
	"""The old `not oc.phases` guard dropped the error whenever a job had
	both a phase history and a top-level error."""
	spec = JobSpec('j1', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'))
	bp = BatchAbaqusProcessor([spec], str(tmp_path), cpus_per_job=1)
	bp._log_job_summary(JobOutcome(
		'j1', 'UNKNOWN_ERROR', error='ValueError: escaped after preparation',
		phases=[{'phase': 'preparation', 'status': 'PREPARATION_SUCCESS',
				'duration_s': 0.5, 'error': None}]))
	for h in bp.logger.handlers:
		h.flush()

	# encoding is explicit: the handler writes UTF-8, and the phase lines use
	# an em-dash, which a default GBK/cp1252 read would choke on.
	with open(os.path.join(str(tmp_path), 'batch_processor.log'), encoding='utf-8') as f:
		content = f.read()
	assert 'escaped after preparation' in content


def test_log_job_summary_does_not_repeat_the_phase_error(tmp_path):
	"""error normally duplicates the failing phase's message — log it once."""
	spec = JobSpec('j1', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'))
	bp = BatchAbaqusProcessor([spec], str(tmp_path), cpus_per_job=1)
	bp._log_job_summary(JobOutcome(
		'j1', 'PREPARATION_FAILED', error='Source INP not found',
		phases=[{'phase': 'preparation', 'status': 'PREPARATION_FAILED',
				'duration_s': 0.5, 'error': 'Source INP not found'}]))
	for h in bp.logger.handlers:
		h.flush()

	with open(os.path.join(str(tmp_path), 'batch_processor.log'), encoding='utf-8') as f:
		content = f.read()
	assert content.count('Source INP not found') == 1


# ============================================================
# Pooled batches: measured cores, oversubscription is a report
# ============================================================

class _ProbeBackend:
	"""Backend stand-in that answers the core-count probe and nothing else."""

	def __init__(self, cores):
		self.cores = cores
		self.probes = 0
		self.closed = False

	def probe_cores(self):
		self.probes += 1
		return self.cores

	def close(self):
		self.closed = True


def _remote_host(name='node01', **kw):
	kw.setdefault('hostname', f'{name}.example')
	kw.setdefault('work_root', r'D:\abqwork')
	kw.setdefault('abaqus_exe', r'C:\SIMULIA\Commands\abaqus.bat')
	return HostSpec(name=name, **kw)


def _processor(tmp_path, hosts, cpus_per_job=2):
	spec = JobSpec('pooled',
				preparation=PreparationSpec(kind='existing_inp',
											source_path='missing.inp'))
	return BatchAbaqusProcessor([spec], str(tmp_path), cpus_per_job=cpus_per_job,
								hosts=hosts)


def _patch_backend(monkeypatch, backend):
	monkeypatch.setattr('ABQflow.core.abaqus_automation.make_backend',
						lambda host=None, logger=None: backend)


def test_a_remote_host_without_cpus_total_is_measured(tmp_path, monkeypatch):
	"""The remote counterpart of reading this machine's cores automatically."""
	backend = _ProbeBackend(32)
	_patch_backend(monkeypatch, backend)
	host = _remote_host()
	bp = _processor(tmp_path, [host])

	assert bp._pool_cores([host]) == {'node01': 32}
	assert backend.probes == 1 and backend.closed is True
	assert host.capacity(bp.cpus_per_job, 32) == (32 - 1) // 2


def test_an_explicit_cpus_total_skips_the_probe(tmp_path, monkeypatch):
	backend = _ProbeBackend(32)
	_patch_backend(monkeypatch, backend)
	host = _remote_host(cpus_total=16)
	bp = _processor(tmp_path, [host])

	assert bp._pool_cores([host]) == {'node01': None}   # resolved_cores uses 16
	assert backend.probes == 0


def test_the_probe_runs_once_per_batch(tmp_path, monkeypatch):
	"""Every phase method goes through _execute_pool; one measurement is enough."""
	backend = _ProbeBackend(32)
	_patch_backend(monkeypatch, backend)
	host = _remote_host()
	bp = _processor(tmp_path, [host])

	bp._pool_cores([host])
	bp._pool_cores([host])
	assert backend.probes == 1


def test_a_local_pool_member_is_never_probed(tmp_path, monkeypatch):
	backend = _ProbeBackend(32)
	_patch_backend(monkeypatch, backend)
	host = HostSpec.local(name='here', max_concurrent=1)
	bp = _processor(tmp_path, [host])

	assert bp._pool_cores([host]) == {'here': None}
	assert backend.probes == 0


def test_an_unmeasurable_host_without_a_cap_is_refused(tmp_path, monkeypatch):
	"""Deriving concurrency from an invented core count would be worse."""
	_patch_backend(monkeypatch, _ProbeBackend(None))
	host = _remote_host()
	bp = _processor(tmp_path, [host])

	with pytest.raises(ValueError, match='cpus_total'):
		bp._pool_cores([host])


def test_an_unmeasurable_host_with_a_cap_runs_anyway(tmp_path, monkeypatch, caplog):
	_patch_backend(monkeypatch, _ProbeBackend(None))
	host = _remote_host(max_concurrent=2)
	bp = _processor(tmp_path, [host])

	with caplog.at_level('WARNING'):
		assert bp._pool_cores([host]) == {'node01': None}
	assert 'unknown' in caplog.text


def test_a_failed_probe_is_not_retried_for_every_phase(tmp_path, monkeypatch):
	backend = _ProbeBackend(None)
	_patch_backend(monkeypatch, backend)
	host = _remote_host(max_concurrent=2)
	bp = _processor(tmp_path, [host])

	bp._pool_cores([host])
	bp._pool_cores([host])
	assert backend.probes == 1


def test_a_pooled_batch_reports_oversubscription_and_still_runs(tmp_path, caplog):
	"""Cores are advisory here exactly as in a host-less batch: warn, then run."""
	host = HostSpec.local(name='here', cpus_total=2, max_concurrent=4)
	bp = _processor(tmp_path, [host], cpus_per_job=4)
	calcs = [bp._build_calc(spec) for spec in bp.specs]

	with caplog.at_level('WARNING'):
		outcomes = bp._execute_pool(calcs, 'prepare', num_parallel_jobs=1)

	assert 'oversubscribes' in caplog.text
	assert '16 cores requested' in caplog.text
	assert len(outcomes) == 1          # the job ran; concurrency was not clipped
