Getting Started
===============

This guide walks you through installation, a single-job run, batch parameter
sweeps, and the output format.

Installation
------------

.. code-block:: bash

   pixi add --pypi "ABQflow @ git+https://github.com/Yutu0k/ABQflow.git"

Prerequisites
-------------

* **Abaqus** installed and the ``abaqus`` command available on ``PATH``.
* **Python 3.9+**.
* **abqpy** (optional, but recommended).  When ``abqpy`` is detected, hook
  scripts run under ``python`` directly instead of ``abaqus python``,
  enabling a standard Python toolchain.

Quick Example: Single Job with InpModifyStrategy
-------------------------------------------------

The simplest modular workflow uses a base INP file with ``{{placeholders}}``
that get replaced per job.

.. code-block:: python

  from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec

  spec = JobSpec(
      job_name = "planar_stress",
      workflow = "modular",
      preparation = PreparationSpec(
          kind = "inp_based",
          source_path = "./examples/SingleParameterizedJob/cae_file/planar_stress_template.inp",
          params = {
              "youngs_modulus": 210000,
              "load_magnitude": 2000,
          }
      ),
      post_extraction = [
          HookSpec(
              script_path = "./examples/SingleParameterizedJob/cae_file/get_max_stress_mises.py",
              tasks = [
                  {"result_name": "max_stress_mises",},
                  {"result_name": "max_displacement",},
              ]
          )
      ]
  )

  processor = BatchAbaqusProcessor(
      batch_data = [spec],
      base_output_dir = ("./examples/SingleParameterizedJob/output"),
      cpus_per_job = 4,
      duplicate_mode = "overwrite",
  )
  outcomes = processor.run_batch(num_parallel_jobs=1)

  for oc in outcomes:
      print(f"{oc.job_name}: {oc.status} → {oc.results}")

Quick Example: Batch with ``generate_from_array``
--------------------------------------------------

Sweep parameters by generating multiple specs from a single base.

.. code-block:: python

  import numpy as np
  from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec
  from ABQflow import generate_from_array, degenerate_from_array

  param_names = ['youngs_modulus', 'load_magnitude']
  param_values = np.array([
  	[200000, 2000],
  	[210000, 3000],
  	[220000, 4000],
  	[230000, 5000]
  ])

  base_job_spec = JobSpec(
      job_name = "planar_stress_batch",
      workflow = "modular",
      preparation = PreparationSpec(
          kind = "inp_based",
          source_path = "./examples/BatchParameterizedJob/cae_file/planar_stress_template.inp",
      ),
      pre_extraction = [
          HookSpec(
              script_path = "./examples/BatchParameterizedJob/cae_file/get_total_mass.py",
              tasks = [
                  {"result_name": "total_mass",},
              ]
          )
      ],
      post_extraction = [
          HookSpec(
              script_path = "./examples/BatchParameterizedJob/cae_file/get_max_stress_mises.py",
              tasks = [
                  {"result_name": "max_stress_mises",},
                  {"result_name": "max_displacement",},
              ]
          )
      ]
  )

  spec_list = generate_from_array(
      samples_array = param_values,
      param_names = param_names,
      base_spec  = base_job_spec
  )

  proc = BatchAbaqusProcessor(spec_list, './output', cpus_per_job=4)
  outcomes = proc.run_batch(num_parallel_jobs=2)

  # Get a 2D numpy array of results
  arr = degenerate_from_array(outcomes = outcomes, output_names = ["total_mass", "max_stress_mises", "max_displacement"])
  print(arr)  # shape (4, 3)

Quick Example: Monolithic Script
---------------------------------

TODO

Output Format: ``JobOutcome``
-----------------------------

Every job returns a :class:`~abaqus_batch_pack.JobOutcome` dataclass:

.. code-block:: python

   @dataclass
   class JobOutcome:
       job_name: str          # e.g. "beam_sweep_0001"
       status: str            # "COMPLETED", "SIMULATION_FAILED", ...
       results: dict | None   # extracted data keyed by result_name
       error:   str | None    # traceback if something went wrong

Converter helpers are available:

* :func:`~abaqus_batch_pack.outcomes_to_list` -- ``list[dict]`` format.
* :func:`~abaqus_batch_pack.outcomes_to_dict` -- ``{job_name: dict}`` format.
* :func:`~abaqus_batch_pack.degenerate_from_array` -- ``numpy.ndarray`` from batch results.

Configuration Reference
-----------------------

**BatchAbaqusProcessor** constructor parameters:

.. list-table::
   :header-rows: 1

   * - Parameter
     - Type
     - Default
     - Description
   * - ``batch_data``
     - ``list[dict] | list[JobSpec]``
     - (required)
     - Job specifications.
   * - ``base_output_dir``
     - ``str``
     - (required)
     - Root directory for job outputs.
   * - ``cpus_per_job``
     - ``int``
     - (required)
     - CPUs allocated to each Abaqus job.
   * - ``abaqus_exe``
     - ``str``
     - ``"abaqus"``
     - Path to the Abaqus executable.
   * - ``duplicate_mode``
     - ``str``
     - ``"fail"``
     - One of ``fail``, ``skip``, ``overwrite``, ``interactive``.
   * - ``prompt_fn``
     - ``callable``
     - ``input``
     - Callback for interactive prompts.
   * - ``timeout``
     - ``float | None``
     - ``None``
     - Seconds before a subprocess call is killed.

**``run_batch``** parameters:

* ``num_parallel_jobs`` -- Requested parallelism. May be reduced by the
  :func:`~abaqus_batch_pack.plan_parallelism` resource planner.
* ``license_tokens`` (optional) -- Total Abaqus license tokens available.
  If provided, parallelism is also capped by token consumption
  (:func:`~abaqus_batch_pack.solver_tokens`).

.. _json_protocol:

Hook Script Conventions
-----------------------

Hook scripts (post-processing scripts that extract data from ODB or INP files)
communicate results back to the framework via JSON on stdout.  Two conventions
are supported:

**Sentinel markers (recommended):**

.. code-block:: python

   import json, sys

   results = {"max_stress": 123.4, "mass": 0.56}
   sys.__stdout__.write("===ABQ_RESULT_BEGIN===\n")
   sys.__stdout__.write(json.dumps(results) + "\n")
   sys.__stdout__.write("===ABQ_RESULT_END===\n")

The framework splits on these markers, ignoring Abaqus banner noise.

**argparse interface for hook scripts:**

The framework invokes hook scripts with these arguments automatically:

* ``--odb_path <path>`` or ``--inp_path <path>`` -- the file to process.
* ``--tasks_json <tmpfile>`` -- path to a temporary JSON file containing a
  list of ``{"result_name": "..."}`` task dicts.  Read each task, run it,
  and collect results into a ``{result_name: value}`` dict for output.

Your script can add custom arguments via ``common_args`` in the hook spec.
