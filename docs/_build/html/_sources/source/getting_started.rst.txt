Getting Started
===============

This guide walks you through installation, a single-job run, batch parameter
sweeps, and the output format.

Installation
------------

.. code-block:: bash

   pixi add --pypi "abq_flow @ git+https://github.com/Yutu0k/abaqus-batch-pack.git"

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

   from abaqus_batch_pack import (
       JobSpec, PreparationSpec, BatchAbaqusProcessor,
   )

   spec = JobSpec(
       job_name="cantilever",
       preparation=PreparationSpec(
           kind="inp_based",
           source_path="base_beam.inp",
           params={"thickness": 0.01, "load": 1000.0},
       ),
   )

   processor = BatchAbaqusProcessor(
       batch_data=[spec],
       base_output_dir="./output",
       cpus_per_job=4,
   )
   outcomes = processor.run_batch(num_parallel_jobs=1)
   print(outcomes[0].status)

Quick Example: Batch with ``generate_from_array``
--------------------------------------------------

Sweep parameters by generating multiple specs from a single base.

.. code-block:: python

   import numpy as np
   from abaqus_batch_pack import (
       JobSpec, PreparationSpec, generate_from_array,
       BatchAbaqusProcessor,
   )

   base = JobSpec(
       job_name="beam_sweep",
       preparation=PreparationSpec(
           kind="inp_based",
           source_path="base_beam.inp",
       ),
   )

   # Sample 5 (thickness, load) pairs
   samples = np.array([
       [0.01, 1000.0],
       [0.02, 1500.0],
       [0.03, 2000.0],
       [0.04, 2500.0],
       [0.05, 3000.0],
   ])

   specs = generate_from_array(samples, ["thickness", "load"], base)
   # specs[0].job_name == "beam_sweep_0001"

   processor = BatchAbaqusProcessor(
       batch_data=specs,
       base_output_dir="./output",
       cpus_per_job=4,
   )
   outcomes = processor.run_batch(num_parallel_jobs=2)

Quick Example: Monolithic Script
---------------------------------

When you prefer a single self-contained Abaqus script:

.. code-block:: python

   from abaqus_batch_pack import JobSpec, BatchAbaqusProcessor

   spec = JobSpec(
       job_name="mono_example",
       workflow="monolithic",
       monolithic_script="my_script.py",
       monolithic_params={"mesh_size": 0.5},
   )

   processor = BatchAbaqusProcessor(
       batch_data=[spec],
       base_output_dir="./output",
       cpus_per_job=4,
   )
   outcomes = processor.run_batch(num_parallel_jobs=1)

Monolithic scripts should output results using the JSON sentinel markers
(see :ref:`json_protocol`).

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
