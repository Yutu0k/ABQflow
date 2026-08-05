"""Recognizing preflight pass/fail against the real Abaqus syntax checker
(mirrors examples/04_PreflightAndDiagnostics).

Opt-in: skipped unless ``pytest --run-abaqus`` is passed (see test/conftest.py).

Run: pixi run pytest test/integration/test_preflight.py --run-abaqus -v
"""

import os

import pytest

from ABQflow import BatchAbaqusProcessor, HookSpec, JobSpec, PreparationSpec

from _paths import GET_MAX_STRESS_SCRIPT, SCENARIO_1_INP

pytestmark = pytest.mark.abaqus

# Contains *STEP and no {{placeholder}} markers (so ExistingInpStrategy accepts
# it) but no valid model definition at all — abaqus syntaxcheck must reject it.
_BROKEN_INP = """\
*Heading
** intentionally invalid INP for preflight-failure recognition
*STEP
*BOGUS_KEYWORD_THAT_DOES_NOT_EXIST, foo=bar
*STATIC
1., 1., 1e-05, 1.
*END STEP
"""


def test_preflight_syntaxcheck_passes_for_valid_inp(tmp_path, abaqus_exe):
	spec = JobSpec(
		job_name="integration_preflight_valid",
		preparation=PreparationSpec(
			kind="existing_inp",
			source_path=SCENARIO_1_INP,
			options={"staging_mode": "copy", "resolve_includes": True},
		),
		preflight="syntaxcheck",
		post_extraction=[
			HookSpec(
				script_path=GET_MAX_STRESS_SCRIPT,
				tasks=[{"result_name": "max_stress_mises"}],
			)
		],
	)
	processor = BatchAbaqusProcessor(
		batch_data=[spec], base_output_dir=str(tmp_path), cpus_per_job=4,
		duplicate_mode="overwrite", abaqus_exe=abaqus_exe, timeout=300,
	)
	outcomes = processor.run_batch(num_parallel_jobs=1)
	oc = outcomes[0]

	assert oc.status == "COMPLETED", f"job failed: {oc.error}"
	phase_names = [p["phase"] for p in oc.phases]
	assert "preflight" in phase_names
	preflight_phase = next(p for p in oc.phases if p["phase"] == "preflight")
	assert preflight_phase["status"] == "PASSED"


def test_preflight_syntaxcheck_fails_for_invalid_inp_and_blocks_solver(tmp_path, abaqus_exe):
	broken_inp_path = tmp_path / "broken.inp"
	broken_inp_path.write_text(_BROKEN_INP)

	spec = JobSpec(
		job_name="integration_preflight_broken",
		preparation=PreparationSpec(
			kind="existing_inp",
			source_path=str(broken_inp_path),
			options={"staging_mode": "copy", "resolve_includes": False},
		),
		preflight="syntaxcheck",
	)
	out_dir = str(tmp_path / "out")
	processor = BatchAbaqusProcessor(
		batch_data=[spec], base_output_dir=out_dir, cpus_per_job=4,
		duplicate_mode="overwrite", abaqus_exe=abaqus_exe, timeout=300,
	)
	outcomes = processor.run_batch(num_parallel_jobs=1)
	oc = outcomes[0]

	assert oc.status == "PREFLIGHT_FAILED", f"expected preflight to fail, got {oc.status}"
	assert [p["phase"] for p in oc.phases] == ["preparation", "preflight"]
	assert oc.phases[-1]["status"] == "PREFLIGHT_FAILED"
	# The real error harvested from Abaqus's own .dat output lives on the
	# phase record — JobOutcome.error is only promoted on solver failures,
	# not preparation/preflight ones.
	assert "bogus_keyword" in oc.phases[-1]["error"].lower()

	# The solver must never have been reached.
	job_dir = os.path.join(out_dir, spec.job_name)
	assert not os.path.isfile(os.path.join(job_dir, f"{spec.job_name}.odb"))


def test_preflight_only_inspector_mode_stops_before_solver(tmp_path, abaqus_exe):
	"""preflight_only=True (batch inspection mode) checks the INP and stops —
	no .odb is ever produced even for an otherwise-valid job."""
	spec = JobSpec(
		job_name="integration_preflight_only",
		preparation=PreparationSpec(
			kind="existing_inp",
			source_path=SCENARIO_1_INP,
			options={"staging_mode": "copy", "resolve_includes": True},
		),
		preflight="syntaxcheck",
	)
	processor = BatchAbaqusProcessor(
		batch_data=[spec], base_output_dir=str(tmp_path), cpus_per_job=4,
		duplicate_mode="overwrite", abaqus_exe=abaqus_exe, timeout=300,
		preflight_only=True,
	)
	processor.prepare()
	outcomes = processor.run_batch(num_parallel_jobs=1)
	oc = outcomes[0]

	assert oc.status == "COMPLETED"
	assert [p["phase"] for p in oc.phases] == ["preparation", "preflight"]

	job_dir = os.path.join(str(tmp_path), spec.job_name)
	assert not os.path.isfile(os.path.join(job_dir, f"{spec.job_name}.odb"))
