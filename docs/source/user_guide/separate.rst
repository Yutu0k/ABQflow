Separate Processing
===================

For ``modular`` workflow, the preparation period, simulation period and extraction period can be separated.

Run Job
-------

.. code-block:: python

    import os
    from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec

    YOUNGS_MODULUS_LIST = [190000, 200000, 210000]

    specs = [
        JobSpec(
            job_name = f"separate_job_{i:02d}",
            workflow = "modular",
            preparation = PreparationSpec(
                kind = "inp_based",
                source_path = "./examples/cae_file/planar_stress_template.inp",
                params = {
                    "youngs_modulus": e,
                    "load_magnitude": 2000,
                }
            ),
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
        for i, e in enumerate(YOUNGS_MODULUS_LIST, start=1)
    ]

    processor = BatchAbaqusProcessor(
        batch_data = specs,
        base_output_dir = OUTPUT_DIR,
        cpus_per_job = 4,
        duplicate_mode = "overwrite",
        abaqus_exe = ABAQUS_CAE,
    )

    prep_outcomes = processor.run_preparation(num_parallel_jobs=3)      # <- Run preparation Here
    sim_outcomes = processor.run_simulation(num_parallel_jobs=3)        # <- Run simulation Here

.. warning::

    If ``pre_extraction`` is configured, it will be executed in :meth:`~ABQflow.BatchAbaqusProcessor.run_simulation`



Post Processing
---------------

Post process is executed in a different script file. First, a same spec_list should be defined:

.. code-block:: python

    import os
    from ABQflow import BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec

    YOUNGS_MODULUS_LIST = [190000, 200000, 210000]

    specs = [
        JobSpec(
            job_name = f"separate_job_{i:02d}",
            workflow = "modular",
            preparation = PreparationSpec(
                kind = "inp_based",
                source_path = "./examples/cae_file/planar_stress_template.inp",
                params = {
                    "youngs_modulus": e,
                    "load_magnitude": 2000,
                }
            ),
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
        for i, e in enumerate(YOUNGS_MODULUS_LIST, start=1)
    ]

    processor = BatchAbaqusProcessor(
        batch_data = specs,
        base_output_dir = OUTPUT_DIR,
        cpus_per_job = 4,
        abaqus_exe = ABAQUS_CAE,
        duplicate_mode = "skip",        # <- Just in case
    )

    ext_outcomes = processor.run_extraction(num_parallel_jobs=3)

The same results should be obtained afterwards.