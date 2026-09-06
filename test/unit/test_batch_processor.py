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
