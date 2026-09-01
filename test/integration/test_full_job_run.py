"""End-to-end run against a real Abaqus solver (mirrors examples/01_SingleParameterizedJob).

Opt-in: skipped unless ``pytest --run-abaqus`` is passed (see test/conftest.py)
and a real Abaqus executable is found (see the ``abaqus_exe`` fixture).
Each job takes roughly a minute on this model — see the timeout below.

Run: pixi run pytest test/integration/test_full_job_run.py --run-abaqus -v
"""

import os

import pytest
from _paths import GET_MAX_STRESS_SCRIPT, TEMPLATE_INP

from ABQflow import (
	BatchAbaqusProcessor,
	HookSpec,
	JobSpec,
	PreparationSpec,
	diagnose,
)

pytestmark = pytest.mark.abaqus


def test_inp_based_job_completes_and_diagnostics_recognize_success(tmp_path, abaqus_exe):
	"""A full inp_based -> solve -> post_extraction run reaches COMPLETED,
	produces real result values, and diagnose() independently confirms the
	solver's own .sta file reports a clean completion."""
	spec = JobSpec(
		job_name="integration_full_job",
		workflow="modular",
		preparation=PreparationSpec(
			kind="inp_based",
			source_path=TEMPLATE_INP,
			params={"youngs_modulus": 210000, "load_magnitude": 2000},
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

	processor = BatchAbaqusProcessor(
		batch_data=[spec],
		base_output_dir=str(tmp_path),
		cpus_per_job=4,
		duplicate_mode="overwrite",
		abaqus_exe=abaqus_exe,
		timeout=300,
	)
	outcomes = processor.run_batch(num_parallel_jobs=1)

	assert len(outcomes) == 1
	oc = outcomes[0]
	assert oc.status == "COMPLETED", f"job failed: {oc.error}"
	assert oc.results["max_stress_mises"] > 0
	assert oc.results["max_displacement"] > 0

	# Status recognition: the phase history must show every phase as *_SUCCESS,
	# in pipeline order.
	phase_names = [p["phase"] for p in oc.phases]
	assert phase_names == ["preparation", "simulation", "post_extraction"]
	for p in oc.phases:
		assert p["status"].endswith("_SUCCESS"), p

	job_dir = os.path.join(str(tmp_path), spec.job_name)
	assert os.path.isfile(os.path.join(job_dir, f"{spec.job_name}.inp"))
	assert os.path.isfile(os.path.join(job_dir, f"{spec.job_name}.odb"))
	assert os.path.isfile(os.path.join(job_dir, f"{spec.job_name}.sta"))

	# Independent recognition path: diagnose() re-derives the verdict straight
	# from the solver's own .sta/.msg files, not from ABQflow's in-memory state.
	diag = diagnose(spec.job_name, job_dir)
	assert diag.sta_verdict == "COMPLETED"
	assert diag.solver_type == "standard"
	assert diag.increments >= 1
	assert diag.errors == []
