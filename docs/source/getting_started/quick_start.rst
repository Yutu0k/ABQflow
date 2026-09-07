Quick Start
===========

After installing ABQflow, you can run a simple batch of jobs. This following simplest modular workflow uses a base INP file with ``{{placeholders}}``
that get replaced per job.

.. code-block:: python

  from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec

  spec = JobSpec(
      job_name = "planar_stress",
      workflow = "modular",
      preparation = PreparationSpec(
          kind = "inp_based",
          source_path = "./examples/cae_file/planar_stress_template.inp",
          params = {
              "youngs_modulus": 210000,
              "load_magnitude": 2000,
          }
      ),
      post_extraction = [
          HookSpec(
              script_path = "./examples/extraction_scripts/get_max_stress_mises.py",
              tasks = [
                  {"result_name": "max_stress_mises",},
                  {"result_name": "max_displacement",},
              ]
          )
      ]
  )

  processor = BatchAbaqusProcessor(
      batch_data = [spec],
      base_output_dir = ("./examples/01_SingleParameterizedJob/output"),
      cpus_per_job = 4,
      duplicate_mode = "overwrite",
  )
  outcomes = processor.run_batch(num_parallel_jobs=1)

  for oc in outcomes:
      print(f"{oc.job_name}: {oc.status} → {oc.results}")

