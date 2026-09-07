Diagnostics
===========

Diagnostics are supported by ``ABQflow`` to help users identify potential issues in their Abaqus input files. 

In ABAQUS cli, we can use ``abaqus job=myjob [option]`` to submit a job. The ``[option]`` for diagnostics can be

* ``syntaxcheck``: Check the syntax of the input file.
* ``datacheck``: Check the data in the input file.
* ``parametercheck``: Check the parameters in the input file.

Preflight checks
----------------

Each diagnostic check should be performed when specifying ``JobSpec``. In this case, preflight checks will be performed before the job is finally submitted to Abaqus. If the preflight check **fails**, the job will not be submitted to Abaqus and immediately return in ``JobOutcome``. 

.. code-block:: python

    from ABQflow import JobSpec, PreparationSpec

    base_spec = JobSpec(
        job_name = "preflight_example",
        workflow = "modular",
        preparation = PreparationSpec(
            kind = "existing_inp",
            source_path = "",
            options = {"resolve_includes": True}
        ),
        preflight = "syntaxcheck",    # <- Specify the preflight check option here
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

    specs = generate_from_inp_files(
        ["./examples/cae_file/scenarios/planar_stress_scenario_1.inp",
        "./examples/cae_file/scenarios/planar_stress_scenario_2.inp"],
        base_spec, naming="stem"
    )

.. note::

    If you don't want to perform the entire solver run, ``preflight_only`` option is given in :class:`~ABQflow.BatchAbaqusProcessor` to only perform preflight checks without running the solver.

    .. code-block:: python

        inspector = BatchAbaqusProcessor(
            batch_data = specs,
            base_output_dir = os.path.join(CWD, "examples/PreflightAndDiagnostics/output_inspect"),
            cpus_per_job = 4,
            duplicate_mode = "overwrite",
            abaqus_exe = ABAQUS_CAE,
            preflight_only = True,
        )

Dry Run
-------

Dry run is a feature in ``ABQflow`` to check the generated commands for each job without actually running the solver. It can be used to verify if the commands are generated correctly and if the job specifications are set up properly. Support two modes:

* ``plan``: no command execution and directory creation

    .. code-block:: python

        plans = processor.dry_run("plan")


* ``stage``: staging INP, no solver and extraction execution

    .. code-block:: python

        plans = processor.dry_run("stage")


