"""Recognizing status across separate preparation/simulation/extraction runs
against a real Abaqus solver (mirrors examples/06_SeparateJob).

Each phase is driven by a freshly constructed BatchAbaqusProcessor pointing
at the same output directory — the way a user would monitor/resume a batch
across separate sessions — and the job's status/phase-history/on-disk
artifacts are checked after each one.

Opt-in: skipped unless ``pytest --run-abaqus`` is passed (see test/conftest.py).

Run: pixi run pytest test/integration/test_stage_separation.py --run-abaqus -v
"""

import os

import pytest

from ABQflow import BatchAbaqusProcessor, HookSpec, JobSpec, PreparationSpec

from _paths import GET_MAX_STRESS_SCRIPT, TEMPLATE_INP

pytestmark = pytest.mark.abaqus


def _make_processor(tmp_path, abaqus_exe):
	spec = JobSpec(
		job_name="integration_stage_job",
		workflow="modular",
		preparation=PreparationSpec(
			kind="inp_based",
			source_path=TEMPLATE_INP,
			params={"youngs_modulus": 200000, "load_magnitude": 2000},
		),
		post_extraction=[
			HookSpec(
				script_path=GET_MAX_STRESS_SCRIPT,
				tasks=[
					{"result_name": "max_stress_mises"},
					{"result_name": "max_displacement"},
				],
			)
		],
	)
	return BatchAbaqusProcessor(
		batch_data=[spec],
		base_output_dir=str(tmp_path),
		cpus_per_job=4,
		duplicate_mode="skip",
		abaqus_exe=abaqus_exe,
		timeout=300,
	), spec


def test_stage_separated_run_recognizes_status_at_each_phase(tmp_path, abaqus_exe):
	processor, spec = _make_processor(tmp_path, abaqus_exe)
	job_dir = os.path.join(str(tmp_path), spec.job_name)
	inp_path = os.path.join(job_dir, f"{spec.job_name}.inp")
	odb_path = os.path.join(job_dir, f"{spec.job_name}.odb")

	# ---- Phase 1: preparation only ----
	prep_outcomes = processor.run_preparation(num_parallel_jobs=1)
	prep_oc = prep_outcomes[0]
	assert prep_oc.status == "COMPLETED", f"preparation failed: {prep_oc.error}"
	assert [p["phase"] for p in prep_oc.phases] == ["preparation"]
	assert os.path.isfile(inp_path)
	assert not os.path.isfile(odb_path)  # solver has not run yet

	# ---- Phase 2: simulation only (fresh processor instance, same output dir) ----
	processor2, _ = _make_processor(tmp_path, abaqus_exe)
	sim_outcomes = processor2.run_simulation(num_parallel_jobs=1)
	sim_oc = sim_outcomes[0]
	assert sim_oc.status == "COMPLETED", f"simulation failed: {sim_oc.error}"
	assert [p["phase"] for p in sim_oc.phases] == ["simulation"]
	assert sim_oc.results == {}  # no pre_extraction hooks configured
	assert os.path.isfile(odb_path)

	# ---- Phase 3: extraction only (fresh processor instance again) ----
	processor3, _ = _make_processor(tmp_path, abaqus_exe)
	ext_outcomes = processor3.run_extraction(num_parallel_jobs=1)
	ext_oc = ext_outcomes[0]
	assert ext_oc.status == "COMPLETED", f"extraction failed: {ext_oc.error}"
	assert [p["phase"] for p in ext_oc.phases] == ["post_extraction"]
	assert ext_oc.results["max_stress_mises"] > 0
	assert ext_oc.results["max_displacement"] > 0
