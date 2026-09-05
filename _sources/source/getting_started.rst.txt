Getting Started
===============

This guide walks you through installation, a single-job run, batch parameter
sweeps, and the output format.

Installation
------------

Install from PyPI:

.. code-block:: bash

   pip install ABQflow

If you want the optional ``abqpy`` integration path:

.. code-block:: bash

   pip install "ABQflow[abqpy]"

If you manage environments with Pixi:

.. code-block:: bash

   pixi add --pypi ABQflow

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
          source_path = "./examples/cae_file/planar_stress_template.inp",
      ),
      pre_extraction = [
          HookSpec(
              script_path = "./examples/extraction_scripts/get_total_mass.py",
              tasks = [
                  {"result_name": "total_mass",},
              ]
          )
      ],
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

  spec_list = generate_from_array(
      samples_array = param_values,
      param_names = param_names,
      base_spec  = base_job_spec
  )

  proc = BatchAbaqusProcessor(spec_list, './examples/02_BatchParameterizedJob/output', cpus_per_job=12)
  outcomes = proc.run_batch(num_parallel_jobs=2)

  # Get a 2D numpy array of results
  arr = degenerate_from_array(outcomes = outcomes, output_names = ["total_mass", "max_stress_mises", "max_displacement"])
  print(arr)  # shape (4, 3)

Quick Example: Monolithic Script
---------------------------------

TODO

Output Format: ``JobOutcome``
-----------------------------

Every job returns a :class:`~ABQflow.JobOutcome` dataclass:

.. code-block:: python

   @dataclass
   class JobOutcome:
       job_name: str          # e.g. "beam_sweep_0001"
       status: str            # "COMPLETED", "SIMULATION_FAILED", ...
       results: dict | None   # extracted data keyed by result_name
       error:   str | None    # traceback if something went wrong

Iterate the list directly, or build ``{oc.job_name: oc for oc in outcomes}``
when you need it keyed by name.  One converter helper is available:

* :func:`~ABQflow.degenerate_from_array` -- ``numpy.ndarray`` from batch results.

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
  :func:`~ABQflow.plan_parallelism` resource planner.
* ``license_tokens`` (optional) -- Total Abaqus license tokens available.
  If provided, parallelism is also capped by token consumption
  (:func:`~ABQflow.solver_tokens`).

License Token Planning
-----------------------

:func:`~ABQflow.solver_tokens` and :func:`~ABQflow.plan_parallelism` let you
work out parallelism ahead of time instead of guessing:

.. code-block:: python

   from ABQflow import solver_tokens, plan_parallelism

   # Tokens for 4 CPUs: ceil(5 * 4^0.422) = 9
   print(solver_tokens(4))  # -> 9

   # With 45 license tokens available, 4 CPUs/job (9 tokens each): capped to 5
   print(plan_parallelism(requested=8, cpus_per_job=4, license_tokens=45))  # -> 5

   # With no license limit, CPU cores are informational only -- oversubscription
   # is allowed, but logs a warning if it exceeds physical core capacity.
   print(plan_parallelism(requested=8, cpus_per_job=4))  # -> 8 (+ warning)

License tokens are a hard cap -- Abaqus refuses to start a job it cannot
license. CPU cores are not: requesting more parallel jobs than physical
cores support is allowed, since small jobs rarely saturate a full core; it
only triggers a warning.

.. _json_protocol:

Hook Script Conventions
-----------------------

Hook scripts (post-processing scripts that extract data from ODB or INP files)
run under the **Abaqus Python interpreter** (``abaqus python`` or
``abaqus cae noGUI``) and communicate results back to the framework via JSON
on stdout. ABQflow provides **hookkit** -- a single-file, stdlib-only harness
that eliminates the boilerplate below; you write only the physics.

**Quick start (ODB):**

.. code-block:: python

   # my_extract.py
   import os, sys
   sys.path.insert(0, os.getcwd())     # hookkit is staged here by ABQflow
   import hookkit

   def extract_one(odb_path, task):
       """Physics in, value out. Raise on failure."""
       from odbAccess import openOdb
       name = task['result_name']

       with hookkit.opened(openOdb(path=odb_path, readOnly=True)) as odb:
           step = odb.steps[task.get('step', list(odb.steps.keys())[-1])]
           frame = step.frames[-1]
           asm = odb.rootAssembly

           if name == 'max_stress_mises':
               vals = frame.fieldOutputs['S'].getSubset(
                   region=asm.elementSets[' ALL ELEMENTS']).values
               return hookkit.scalar(max(v.mises for v in vals))

           raise ValueError("unsupported result_name: %s" % name)

   if __name__ == '__main__':
       hookkit.run(extract_one, source_arg='--odb_path')

**Quick start (INP / mdb):**

.. code-block:: python

   # my_mass_extract.py
   import os, sys
   sys.path.insert(0, os.getcwd())
   import hookkit

   def extract_one(inp_path, task):
       from abaqus import mdb
       name = task['result_name']

       mdb.ModelFromInputFile(name='_hook_temp', inputFileName=inp_path)
       if 'Model-1' in mdb.models:
           del mdb.models['Model-1']

       root_assembly = mdb.models['_hook_temp'].rootAssembly
       region = root_assembly.sets['ALL'].elements

       if name == 'total_mass':
           mass = root_assembly.getMassProperties(regions=region)['mass']
           return hookkit.scalar(mass)

       raise ValueError("unsupported result_name: %s" % name)

   if __name__ == '__main__':
       hookkit.run(extract_one, source_arg='--inp_path')

**Field output (large data -> CSV sidecar):**

For field quantities (stress tensors, displacement fields), use
``hookkit.field()``. The mode is controlled by ``"output"`` in the task
dict -- ``"inline"`` always returns through stdout JSON, ``"file"`` always
writes a CSV and returns a lightweight envelope, and leaving it unset lets
hookkit decide automatically (>10k rows or >1MB -> file, else inline):

.. code-block:: python

   HookSpec(
       script_path = "./hooks/get_stress_field.py",
       tasks = [{"result_name": "stress_field", "output": "file"}]
   )

Only ``result_name`` is required in a task dict; every other key
(``output``, ``step``, ``columns``, ...) is user-defined and read via
``task.get()`` inside your ``extract_one``.

**Underlying protocol:**

hookkit implements the following conventions for you; write to them
directly only if you need a custom, non-Python-2/3-compatible harness.

*Sentinel markers:*

.. code-block:: python

   import json, sys

   results = {"max_stress": 123.4, "mass": 0.56}
   sys.__stdout__.write("===ABQ_RESULT_BEGIN===\n")
   sys.__stdout__.write(json.dumps(results) + "\n")
   sys.__stdout__.write("===ABQ_RESULT_END===\n")

The framework splits on these markers, ignoring Abaqus banner noise.

*argparse interface for hook scripts:*

The framework invokes hook scripts with these arguments automatically:

* ``--odb_path <path>`` or ``--inp_path <path>`` -- the file to process.
* ``--tasks_json <tmpfile>`` -- path to a temporary JSON file containing a
  list of ``{"result_name": "..."}`` task dicts.  Read each task, run it,
  and collect results into a ``{result_name: value}`` dict for output.

Your script can add custom arguments via ``common_args`` in the hook spec.
