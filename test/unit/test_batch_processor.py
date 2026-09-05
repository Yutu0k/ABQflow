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
