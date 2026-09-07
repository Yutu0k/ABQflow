Inp Extraction Hook
===================

Inp Extraction Hook deals with the extraction of results from Abaqus inp files. The following script gives an example of inp extraction.

We first determine :class:`~ABQflow.HookSpec` object in :class:`~ABQflow.JobSpec` to specify the extraction script and the tasks to be performed.

.. code-block:: python
    
    spec2 = JobSpec(
        job_name = "planar_stress_mass",
        workflow = "modular",
        preparation = PreparationSpec(
            kind = "inp_based",
            source_path = "./examples/cae_file/planar_stress_template.inp",
            params = {
                "youngs_modulus": 210000,
                "load_magnitude": 2000,
            }
        ),
        pre_extraction = [          # <- Inp Extraction Hook shoule be specified in pre_extraction
            HookSpec(
                script_path = "./examples/extraction_scripts/get_total_mass.py",
                tasks = [
                    {"result_name": "total_mass",},
                ]
            )
        ]
    )

.. note::

    Inp Extraction Hook should be specified in ``pre_extraction`` attribute of :class:`~ABQflow.JobSpec` object. It will be executed before the simulation is run.

In the extraction script, we can use :mod:`hookkit` to simplify the extraction process. The following example shows how to extract the total mass from the inp file.

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