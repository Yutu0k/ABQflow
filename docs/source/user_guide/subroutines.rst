Subroutines
===========

It is often the case for us to perform simulations with **USER-Subroutines** in ABAQUS. ABQflow supports the usage of subroutines in a batch. The subroutine file can be specified in :class:`~ABQflow.JobSpec` using ``subroutine`` attribute by :class:`~ABQflow.SubroutineSpec` object. 

**USER-Subroutines** might needs compilation before being used in ABAQUS via ``abaqus make`` command. 

The following example utilizes an linear elastic UMAT subroutine to perform a batch of simulations. The subroutine file is located in ``./examples/07_SubroutineJob/subroutine/umat_elastic.for``.

Define JobSpec
--------------

.. code-block:: python

    import os

    from ABQflow import (
        BatchAbaqusProcessor, JobSpec, PreparationSpec, HookSpec, SubroutineSpec,
    )

    YOUNGS_MODULUS_LIST = [190000, 200000, 210000]

    specs = [
        JobSpec(
            job_name = f"umat_job_{i:02d}",
            workflow = "modular",
            preparation = PreparationSpec(
                kind = "inp_based",
                source_path = "./examples/cae_file/planar_stress_umat_template.inp",
                params = {
                    "youngs_modulus": e,
                    "load_magnitude": 2000,
                }
            ),
            subroutine = SubroutineSpec(
                source_path = UMAT_SOURCE,
                language = "fortran",
                solver = "standard",
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
        for i, e in enumerate(YOUNGS_MODULUS_LIST, start=1)
    ]

    processor = BatchAbaqusProcessor(
        batch_data = specs,
        base_output_dir = OUTPUT_DIR,
        cpus_per_job = 4,
        duplicate_mode = "overwrite",
        abaqus_exe = ABAQUS_CAE,
    )

    outcomes = processor.run_batch(num_parallel_jobs=3)

.. note::

    We can use :meth:`~ABQflow.BatchAbaqusProcessor.dry_run` to chech if commands are generated correctly.

    .. code-block:: python
        
        plans = processor.dry_run("plan")