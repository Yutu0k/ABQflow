Odb Extraction Hook
===================

Odb Extraction Hook deals with the extraction of results from Abaqus ODB files. The following script gives an example of ODB extraction.

Example
-------

We first determine :class:`~ABQflow.HookSpec` object in :class:`~ABQflow.JobSpec` to specify the extraction script and the tasks to be performed. 

.. code-block:: python

    from ABQflow import JobSpec, PreparationSpec, HookSpec

    base_spec = JobSpec(
        job_name = "planar_stress_odb",
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
                    {"result_name": "max_stress_mises"},
                    {"result_name": "max_displacement"},
                ],
                source = "odb",  # <- Specify the source of the extraction
            )
        ]
    )

In the extraction script, we can use :mod:`hookkit` to simplify the extraction process. The following example shows how to extract the maximum Mises stress from the ODB file.

.. code-block:: python

    import os, sys
    sys.path.insert(0, os.getcwd())
    import hookkit          # <- hookkit is a utility library for writing extraction hooks

    def extract_one(odb_path, task):            # <- task corresponds to one of the tasks in :class:`~ABQflow.HookSpec` object
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

``hookkit`` Usage
-----------------

``hookkit`` is a utility library for writing and executing extraction hooks. It provides a simple interface for defining extraction logic and handling the input and output of the extraction process. The main function in hookkit is :func:`~hookkit.run`, which takes an extraction function and runs it with the appropriate arguments.

``hookkit`` also provides field output: or field quantities (stress tensors, displacement fields), use :func:`~hookkit.field()`. 

.. code-block:: python

    def extract_one(odb_path, task):
        from odbAccess import openOdb
        name = task['result_name']

        with hookkit.opened(openOdb(path=odb_path, readOnly=True)) as odb:
            frame = odb.steps['Step-1'].frames[-1]

            if name == 'stress_field':
                vals = frame.fieldOutputs['S'].values
                rows = [[v.elementLabel, v.mises] for v in vals]
                columns = task.get('columns', ['element_label', 'mises_stress'])
                return hookkit.field(task, rows, columns)

            raise ValueError("unsupported result_name: %s" % name)

Normally, if unstated, field output will be returned as stdout JSON. Users can specify in :class:`~ABQflow.HookSpec`:

.. list-table:: Output behavior control
    :header-rows: 1
    :widths: 20 60

    * - ``"output"``
      - Behavior
    * - ``"inline"``
      - Return through stdout JSON (always)
    * - ``"file"``
      - Write CSV + return a lightweight envelope (always)
    * - (unset)
      - Auto: >10k rows or >1MB → file, else inline



.. code-block:: python

    HookSpec(
        script_path = "./hooks/get_stress_field.py",
        tasks = [{"result_name": "stress_field", "output": "file"}]
    )

Your hook receives tasks as ``task`` dicts. Only ``result_name`` is required; every other key is user-defined and read by your ``extract_one`` via ``task.get()``.

.. list-table:: Task dict keys
    :header-rows: 1
    :widths: 20 10 30 40

    * - Key
      - Required
      - Used by
      - Purpose
    * - ``result_name``
      - **yes**
      - hookkit + your code
      - Result key in the output dict; file name for sidecar CSV
    * - ``output``
      - no
      - ``hookkit.field()``
      - ``"inline"`` / ``"file"`` — controls field representation
    * - ``columns``
      - no
      - your code + ``hookkit.field()``
      - CSV column headers for field output
    * - ``*(any other)*``
      - no
      - your code
      - Freely defined — hookkit passes everything through transparently
