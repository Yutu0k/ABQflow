Basic Batch Processing
======================

The following gives a basic pipeline of batch processing. The pipeline utilizes **a template Abaqus input file** and **two extraction scripts**. 

Definition of Batch Job Specs
-----------------------------

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
        job_name = "planar_stress_multiple",
        workflow = "modular",
        preparation = PreparationSpec(
            kind = "inp_based",
            source_path = "./examples/cae_file/planar_stress_template.inp",  # <- The input file to be handled
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
                    {"result_name": "max_stress_mises"},
                    {"result_name": "max_displacement"},
                ]
            )
        ]
    )

After defining a ``base_spec``, a batch of ``spec_list`` can be generated using :func:`~ABQflow.generate_from_array` method:

.. code-block:: python

    spec_list = generate_from_array(
        samples_array = param_values,
        param_names = param_names,
        base_spec  = base_job_spec
    )

``JobSpec`` params
~~~~~~~~~~~~~~~~~~

:class:`~ABQflow.JobSpec` is the single-job configuration. It validates itself at
construction, so an invalid combination raises before any Abaqus process is
launched.

.. list-table::
   :header-rows: 1
   :widths: 20 20 14 46

   * - Parameter
     - Type
     - Default
     - Description
   * - ``job_name``
     - ``str``
     - (required)
     - Unique name for this job; also the name of its working directory under
       ``base_output_dir``, and the key echoed back as
       :attr:`~ABQflow.JobOutcome.job_name`.
   * - ``workflow``
     - ``str``
     - ``"modular"``
     - ``"modular"`` runs the 4-phase pipeline (preparation → preflight →
       simulation → extraction); ``"monolithic"`` hands everything to a single
       script.
   * - ``preparation``
     - ``PreparationSpec | None``
     - ``None``
     - How the INP is produced. **Required for** ``workflow="modular"``;
       ignored for monolithic. See :ref:`preparation_spec`.
   * - ``preflight``
     - ``str | None``
     - ``None``
     - ``None`` (no check), ``"syntaxcheck"``, or ``"datacheck"``. Runs before
       the solver and fails the job with ``PREFLIGHT_FAILED`` if the deck is
       rejected.
   * - ``monolithic_script``
     - ``str | None``
     - ``None``
     - Path to the single script. **Required for**
       ``workflow="monolithic"``.
   * - ``monolithic_params``
     - ``dict``
     - ``{}``
     - Forwarded to that script as ``--key value`` command-line arguments.
   * - ``pre_extraction``
     - ``list[HookSpec]``
     - ``[]``
     - Hooks run *before* the solver — model properties read from the INP
       through the CAE kernel (mass, volume, node counts).
   * - ``post_extraction``
     - ``list[HookSpec]``
     - ``[]``
     - Hooks run *after* the solver. Each entry's ``source`` picks the artifact
       it reads; hooks execute in declaration order.
   * - ``subroutine``
     - ``SubroutineSpec | None``
     - ``None``
     - User subroutine to compile and pass via ``user=`` to the solver. Modular
       workflow only — see :doc:`subroutines`.
   * - ``meta``
     - ``dict``
     - ``{}``
     - Arbitrary user metadata. Carried along untouched, so it is the place to
       stash the parameter set or sample index behind a job.

.. _preparation_spec:

``PreparationSpec`` params
~~~~~~~~~~~~~~~~~~~~~~~~~~

:class:`~ABQflow.PreparationSpec` describes how this job's INP comes into
existence.

.. list-table::
   :header-rows: 1
   :widths: 20 16 14 50

   * - Parameter
     - Type
     - Default
     - Description
   * - ``kind``
     - ``str``
     - (required)
     - ``"inp_based"``, ``"existing_inp"``, or ``"model_generation"``. See :ref:`preparation_spec_kind`.
   * - ``source_path``
     - ``str``
     - ``""``
     - The template or finished INP (``inp_based`` / ``existing_inp``), or the
       model-generation script (``model_generation``). **Leave it empty when a
       generator supplies it** — :func:`~ABQflow.generate_from_inp_files` fills
       it in per file and warns that a value you set is being discarded.
   * - ``params``
     - ``dict``
     - ``{}``
     - ``{{placeholder}}`` replacements for ``inp_based``, CLI arguments for
       ``model_generation``. Unused by ``existing_inp``, which warns if you set
       it. Left empty when :func:`~ABQflow.generate_from_array` supplies it, as
       in the example above.
   * - ``options``
     - ``dict``
     - ``{}``
     - Extra options; see :ref:`preparation_spec_options`. **Unknown keys raise**.

.. _preparation_spec_kind:

.. list-table:: PreparationSpec ``kind`` values
   :header-rows: 1
   :widths: 24 30 46

   * - ``kind``
     - Pair with
     - What varies
   * - ``"inp_based"``
     - :func:`~ABQflow.generate_from_array`, or set ``params`` yourself
     - One template, N parameter sets. A leftover ``{{placeholder}}`` is
       reported as a missing parameter.
   * - ``"existing_inp"``
     - :func:`~ABQflow.generate_from_inp_files`, which fills ``kind`` and
       ``source_path`` in for you
     - N already-written decks. Uses no ``params``; a leftover
       ``{{placeholder}}`` is reported as "you handed a template to a batch of
       finished decks".
   * - ``"model_generation"``
     - ``params`` as script arguments
     - A CAE script builds the model, receiving ``params`` as CLI arguments.

.. _preparation_spec_options:

.. list-table:: PreparationSpec ``options`` keys
   :header-rows: 1
   :widths: 22 14 64

   * - Key
     - Default
     - Meaning
   * - ``resolve_includes``
     - ``True``
     - Whether to walk and rewrite the ``*INCLUDE`` tree. See
       :mod:`ABQflow.core.inp_include` for what the walk does with
       parameterized versus static includes.
   * - ``include_staging``
     - ``"reference"``
     - What to do with the *static* includes. ``"reference"`` leaves them in
       place and points the deck at their absolute paths, so a shared mesh is
       never copied. Parameterized includes have no such choice — their content
       exists nowhere on disk, so they are always written into the job
       directory.

``HookSpec`` params
~~~~~~~~~~~~~~~~~~~

:class:`~ABQflow.HookSpec` binds one extraction script to the results it is expected to produce. The same class is used for ``pre_extraction`` and ``post_extraction``.

.. list-table::
   :header-rows: 1
   :widths: 20 18 14 48

   * - Parameter
     - Type
     - Default
     - Description
   * - ``script_path``
     - ``str``
     - (required)
     - Path to the Python script that performs the extraction.
   * - ``tasks``
     - ``list[dict]``
     - ``[]``
     - One descriptor per value to extract. Only ``result_name`` is required;
       every other key is user-defined and read by your script.
   * - ``source``
     - ``str``
     - ``"odb"``
     - Which artifact the hook reads — ``"odb"`` or ``"dat"``. Consulted for
       ``post_extraction`` only; ``pre_extraction`` always reads the INP through
       the CAE kernel.

Each entry of ``tasks`` is passed through to your script untouched:

.. list-table::
   :header-rows: 1
   :widths: 20 12 24 44

   * - Key
     - Required
     - Used by
     - Purpose
   * - ``result_name``
     - **yes**
     - ``hookkit`` + your code
     - Key this value appears under in :attr:`~ABQflow.JobOutcome.results`, and
       the file name of a sidecar CSV.
   * - ``output``
     - no
     - :func:`~hookkit.field`
     - ``"inline"`` or ``"file"`` — forces the field representation instead of
       the size-based default.
   * - ``columns``
     - no
     - your code + :func:`~hookkit.field`
     - CSV column headers for field output.
   * - *(any other)*
     - no
     - your code
     - Freely defined — read it with ``task.get()`` inside ``extract_one``.

See :doc:`extraction_hook` for how to write the scripts themselves.

Submitting Batch Jobs
---------------------

.. code-block:: python

    processor = BatchAbaqusProcessor(
        batch_data = spec_list,
        base_output_dir = os.path.join(CWD, "examples/02_BatchParameterizedJob/output"),
        cpus_per_job = 12,
        duplicate_mode = "overwrite",
        abaqus_exe = ABAQUS_CAE,  # <- Specify the path to the Abaqus executable
    )
    outcomes = processor.run_batch(num_parallel_jobs=2)

You would see each job being submitted to Abaqus in the console output. After all jobs are completed, the ``outcomes`` list will contain the results of each job.

``BatchAbaqusProcessor`` params
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

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

* ``num_parallel_jobs`` -- Requested parallelism. 
* ``license_tokens`` (optional) -- Total Abaqus license tokens available. If provided, parallelism is also capped by token consumption (:func:`~ABQflow.solver_tokens`).

Processing the Results
----------------------

:meth:`~ABQflow.BatchAbaqusProcessor.run_batch` returns a list of :class:`~ABQflow.JobOutcome` objects. Each :class:`~ABQflow.JobOutcome` object holds: 

.. list-table::
   :header-rows: 1
   :widths: 14 16 70

   * - Attribute
     - Type
     - When it holds what
   * - ``job_name``
     - ``str``
     - Always set — echoes ``JobSpec.job_name``. The join key for a batch.
   * - ``status``
     - ``str``
     - Always set; a plain string, never the enum (it has to survive pickling across process boundaries). See :ref:`status`.
   * - ``results``
     - ``dict | None``
     - Extracted values keyed by ``result_name``. **An empty dict** when the job failed before extraction — not ``None``. ``None`` only when an exception escaped the worker. A value may itself be ``None`` (that
       task failed) or a *sidecar envelope* dict (see below).
   * - ``error``
     - ``str | None``
     - ``None`` on success. On failure, the **first** terminal failure's message — later phases are skipped, so this is the root cause, not the last symptom. For ``UNKNOWN_ERROR`` it is a formatted traceback.
   * - ``diagnostics``
     - ``dict | None``
     - Solver snapshot. **``None`` on a clean success** — it is attached only
       when something is wrong. See :ref:`diagnostics`.
   * - ``output_dir``
     - ``str | None``
     - This job's directory. Needed by :func:`~ABQflow.load_field` to resolve sidecar files; ``None`` only for hand-built outcomes.
   * - ``phases``
     - ``list[dict] | None``
     - Per-phase history for modular workflows. **``None`` for monolithic workflows**, which record no phases. See :ref:`phases`.
   * - ``duration_s``
     - ``float | None``
     - Wall-clock seconds inside the job's ``execute()``. ``None`` when the worker died without reporting.

Iterate the list directly, or build ``{oc.job_name: oc for oc in outcomes}`` when you need it keyed by name.


.. _status:

``status`` values
~~~~~~~~~~~~~~~~~

.. list-table::
   :header-rows: 1
   :widths: 32 68

   * - Value
     - Meaning
   * - ``COMPLETED``
     - Success — the only non-failure value an outcome can carry.
   * - ``SUBROUTINE_COMPILE_FAILED``
     - ``abaqus make`` failed. Note ``abaqus make`` can exit 0 on a broken
       build, so its output is also scanned for driver-level errors; the full
       compiler output is in ``error``.
   * - ``PREPARATION_FAILED``
     - No INP could be produced (missing source, unresolved
       ``{{placeholders}}``, no ``*STEP``).
   * - ``PREFLIGHT_FAILED``
     - The ``syntax``/``datacheck`` preflight rejected the INP.
   * - ``SIMULATION_FAILED``
     - The solver failed. Check ``diagnostics`` first.
   * - ``EXTRACTION_FAILED``
     - A post-extraction task returned ``None``. ``error`` names which ones;
       ``results`` still holds the tasks that did succeed.
   * - ``MONOLITHIC_SCRIPT_FAILED``
     - A monolithic script exited non-zero.
   * - ``JSON_DECODE_ERROR``
     - A script's output could not be parsed — usually missing
       ``===ABQ_RESULT_BEGIN===`` / ``===ABQ_RESULT_END===`` markers.
   * - ``SCRIPT_ERROR``
     - An unhandled exception inside a hook or monolithic script.
   * - ``UNKNOWN_ERROR``
     - An exception escaped the worker. ``error`` holds the traceback — this
       one is a bug report, not a modelling problem.
   * - ``UNKNOWN``
     - A workflow returned no ``status`` at all. Should not occur.

In-progress values (``PREPARING``, ``SIMULATING``, ``EXTRACTING``) and
per-phase success values (``PREPARATION_SUCCESS``, ``COMPILED``, ``PASSED``,
…) appear inside ``phases``, never as a final ``status``.

.. _diagnostics:

``diagnostics`` keys
~~~~~~~~~~~~~~~~~~~~

Diagnostics are harvested after *every* solver run, but only **attached to the
outcome when the run was not clean**.

.. list-table:: Simulation Cases for Diagnostics
   :header-rows: 1
   :widths: 46 54

   * - Situation
     - ``diagnostics``
   * - Solver ran, ``rc == 0``, ``.sta`` says COMPLETED
     - ``None`` — **the normal successful run**
   * - Solver ran and failed
     - Populated; ``errors[0]`` is also copied into ``error``
   * - ``rc != 0`` but ``.sta`` says COMPLETED
     - Populated, while ``status`` is still ``COMPLETED`` and ``error`` is
       ``None``. Diagnostics appearing on a ``COMPLETED`` job *is* the signal
       — read ``errors`` and ``warning_total`` before trusting the results.
   * - Failed before the solver (compile / preparation / preflight)
     - ``None`` — nothing ran, so there is nothing to harvest
   * - Remote staging failed
     - Populated but **empty** (``sta_verdict='INDETERMINATE'``, no errors):
       the job never started, so ``error`` is the informative field
   * - Monolithic workflow
     - ``None`` always — it has no separate solver phase
   * - ``record_only`` dry run
     - ``None`` — nothing was executed

``diagnostics`` are read from the solver's ``.sta`` and ``.msg`` files. Its attributes are:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Key
     - Value
   * - ``sta_verdict``
     - ``COMPLETED`` | ``NOT_COMPLETED`` | ``ABORTED`` | ``INDETERMINATE``
       (the last means no ``.sta`` verdict line was found).
   * - ``errors``
     - Deduplicated, truncated ERROR lines — usually the fastest read.
   * - ``error_total`` / ``warning_total``
     - Counts before dedup/truncation.
   * - ``increments``
     - Completed increments (best-effort). A low count with
       ``NOT_COMPLETED`` points at early divergence.
   * - ``source_files``
     - ``{kind: absolute path}`` for each file actually read.
   * - ``solver_type``
     - ``standard`` | ``explicit`` | ``unknown``.

.. _phases:

``phases`` entries
~~~~~~~~~~~~~~~~~~

Each entry is ``{phase, status, started_at, ended_at, duration_s, error}``.
``phase`` is one of ``compile``, ``preparation``, ``preflight``,
``pre_extraction``, ``simulation``, ``post_extraction``. Only phases that ran
appear, so the last entry is where a failed job stopped.

Reading results
~~~~~~~~~~~~~~~

A large result is written beside the job as a CSV and referenced by a sidecar
envelope (``{"__file__": ..., "format": ..., "shape": ...}``) rather than
inlined. These helpers accept both inline values and envelopes, so you do not
need to care which you got:

* :func:`~ABQflow.load_field` -- one field of one outcome as ``numpy.ndarray``.
* :func:`~ABQflow.iter_fields` -- ``(job_name, ndarray)`` across a batch.
* :func:`~ABQflow.degenerate_from_array` -- 2D ``numpy.ndarray`` from scalar
  results across a batch. Non-``COMPLETED`` jobs become ``default_value`` (``nan``) rows with a warning, so array rows stay aligned with jobs.

``load_field`` and ``iter_fields`` need ``output_dir`` to find a sidecar file;
they warn and return ``None`` if it is missing or the file has been moved.