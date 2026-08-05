"""Tests for ABQflow.core.strategies + registry.build_workflow.

Covers ModularWorkflowStrategy's phase-separated protocol
(prepare_only/simulate_only/extract_only), AbaqusCalculation's phase
dispatch, and subroutine compile-strategy wiring — all with a fake,
subprocess-free PreparationStrategy and record_only=True runners, so no
Abaqus process is ever launched.

Run: pytest test/unit/test_strategies.py -v
"""

import os

import pytest

from ABQflow import (
	AbaqusCalculation,
	AbaqusRunner,
	HookSpec,
	JobContext,
	JobSpec,
	JobStatus,
	ModularWorkflowStrategy,
	MonolithicWorkflowStrategy,
	OdbExtractionStrategy,
	PreparationSpec,
	PreparationStrategy,
	SubroutineCompileStrategy,
	SubroutineSpec,
	build_workflow,
)


class _FakePrep(PreparationStrategy):
	"""Minimal PreparationStrategy — writes a trivial INP, no subprocess calls."""

	def __init__(self, should_succeed=True):
		self.should_succeed = should_succeed

	def prepare(self, ctx, runner, logger):
		if not self.should_succeed:
			return False
		with open(ctx.inp_path, 'w') as f:
			f.write('*STEP\n*END STEP\n')
		return True


# ============================================================
# Log-path collision fix (AbaqusCalculation logging)
# ============================================================

def test_abaqus_calculation_logs_to_exec_log_path_not_native_log(tmp_path):
	"""AbaqusCalculation's FileHandler writes exec_log_path, never log_path."""
	out_dir = str(tmp_path / 'job_log')
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])
	calc = AbaqusCalculation('job_log', out_dir, wf, cpus_per_job=1)
	calc.execute(phase='prepare')
	assert os.path.isfile(calc.ctx.exec_log_path)
	assert not os.path.isfile(calc.ctx.log_path)  # Abaqus never ran, native log absent


# ============================================================
# Phase separation: ModularWorkflowStrategy.prepare_only/simulate_only/extract_only
# ============================================================

def test_modular_workflow_prepare_simulate_extract_only_compose_like_execute(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job1')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job1', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])

	results, sm = wf.prepare_only(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert os.path.isfile(ctx.inp_path)
	assert [p['phase'] for p in results['_phase_history']] == ['preparation']

	sim_results, sm, stop = wf.simulate_only(ctx, runner, dummy_logger, status_manager=sm)
	assert stop is False
	assert sim_results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in sim_results['_phase_history']] == ['preparation', 'simulation']

	ext_results, sm = wf.extract_only(ctx, runner, dummy_logger, status_manager=sm)
	assert ext_results['status'] == JobStatus.COMPLETED  # no post_extraction hooks configured


def test_modular_workflow_simulate_only_missing_inp_stops_pipeline(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job2')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job2', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])

	results, sm, stop = wf.simulate_only(ctx, runner, dummy_logger)
	assert stop is True
	assert results['status'] == JobStatus.SIMULATION_FAILED


def test_modular_workflow_extract_only_missing_odb_fails(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job3')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job3', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	post = [OdbExtractionStrategy([HookSpec('dummy_script.py', [{'result_name': 'mass'}])])]
	wf = ModularWorkflowStrategy(_FakePrep(), [], post)

	results, sm = wf.extract_only(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.EXTRACTION_FAILED
	assert results['mass'] is None


def test_modular_workflow_execute_preflight_only_stops_before_solver(tmp_path, dummy_logger):
	out_dir = str(tmp_path / 'job5')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job5', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [], preflight_mode='syntaxcheck', preflight_only=True)

	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in results['_phase_history']] == ['preparation', 'preflight']
	assert not os.path.exists(ctx.odb_path)  # solver never ran


def test_abaqus_calculation_execute_phase_dispatch(tmp_path):
	out_dir = str(tmp_path / 'jobX')
	wf = ModularWorkflowStrategy(_FakePrep(), [], [])
	calc = AbaqusCalculation('jobX', out_dir, wf, cpus_per_job=1)
	results = calc.execute(phase='prepare')
	assert results['status'] == JobStatus.COMPLETED
	assert os.path.isfile(calc.ctx.inp_path)


def test_abaqus_calculation_execute_phase_not_implemented_for_monolithic(tmp_path):
	out_dir = str(tmp_path / 'jobY')
	wf = MonolithicWorkflowStrategy('nonexistent_script.py', {})
	calc = AbaqusCalculation('jobY', out_dir, wf, cpus_per_job=1)
	with pytest.raises(NotImplementedError):
		calc.execute(phase='prepare')


def test_workflow_preflight_mode():
	spec = JobSpec('pf_test',
				preparation=PreparationSpec(kind='existing_inp', source_path='dummy.inp'),
				preflight='syntaxcheck')
	wf = build_workflow(spec)
	assert wf.preflight_mode == 'syntaxcheck'
	assert wf.preflight_only is False

	wf2 = build_workflow(spec, preflight_only=True)
	assert wf2.preflight_only is True


# ============================================================
# Subroutine support: workflow wiring
# ============================================================

def test_build_workflow_wires_subroutine_into_compile_strategy():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'),
					subroutine=SubroutineSpec('umat.for'))
	wf = build_workflow(spec)
	assert isinstance(wf.compile_strategy, SubroutineCompileStrategy)
	assert wf.compile_strategy.subroutine.source_path == 'umat.for'


def test_build_workflow_without_subroutine_compile_strategy_is_none():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'))
	wf = build_workflow(spec)
	assert wf.compile_strategy is None


def test_subroutine_compile_strategy_precompiled_skips_compile(tmp_path, dummy_logger):
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	strat = SubroutineCompileStrategy(SubroutineSpec('umat.for', precompiled=True))
	ok, msg = strat.compile(ctx, runner, dummy_logger)
	assert ok is True and msg == ''
	assert runner.command_log == []  # never attempted to compile


def test_subroutine_compile_strategy_runs_make_via_record_only(tmp_path, dummy_logger):
	src = tmp_path / 'umat.for'
	src.write_text('C dummy umat source')
	ctx = JobContext(job_name='j', output_dir=str(tmp_path), cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	strat = SubroutineCompileStrategy(SubroutineSpec(str(src)), cache=False)

	ok, msg = strat.compile(ctx, runner, dummy_logger)
	assert ok is True
	assert runner.command_log[-1].stage == 'compile'


def test_modular_workflow_execute_runs_compile_before_preparation(tmp_path, dummy_logger):
	src = tmp_path / 'umat.for'
	src.write_text('C dummy')
	out_dir = str(tmp_path / 'job4')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job4', output_dir=out_dir, cpus=1, user_subroutine=str(src))
	runner = AbaqusRunner(ctx, dummy_logger, record_only=True)
	compile_strat = SubroutineCompileStrategy(SubroutineSpec(str(src)), cache=False)
	wf = ModularWorkflowStrategy(_FakePrep(), [], [], compile_strategy=compile_strat)

	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.COMPLETED
	assert [p['phase'] for p in results['_phase_history']] == ['compile', 'preparation', 'simulation']


def test_subroutine_compile_strategy_failure_stops_pipeline(tmp_path, dummy_logger):
	"""A failing compile step must short-circuit before preparation runs."""
	out_dir = str(tmp_path / 'job6')
	os.makedirs(out_dir, exist_ok=True)
	ctx = JobContext(job_name='job6', output_dir=out_dir, cpus=1)
	runner = AbaqusRunner(ctx, dummy_logger, record_only=False)  # real run_compile -> will fail (no real abaqus/source)

	class _FailingCompile:
		def compile(self, ctx, runner, logger):
			return False, "compiler error: undefined symbol"

	wf = ModularWorkflowStrategy(_FakePrep(), [], [], compile_strategy=_FailingCompile())
	results = wf.execute(ctx, runner, dummy_logger)
	assert results['status'] == JobStatus.SUBROUTINE_COMPILE_FAILED
	assert not os.path.isfile(ctx.inp_path)  # preparation never ran
