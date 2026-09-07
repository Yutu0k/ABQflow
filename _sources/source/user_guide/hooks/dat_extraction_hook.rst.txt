Dat Extraction Hook
===================


When the ODB is too large to open comfortably, the usual workaround is to add `*NODE PRINT` / `*EL PRINT` to the deck so Abaqus also writes the values as text into `<job>.dat`.

Example
-------

We first determine :class:`~ABQflow.HookSpec` object in :class:`~ABQflow.JobSpec` to specify the extraction script and the tasks to be performed.

.. code-block:: python

    spec3 = JobSpec(
        job_name = "planar_stress_dat",
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
                script_path = "./examples/extraction_scripts/get_dat_results.py",
                source = "dat",         # <- Specify the source of the extraction
                tasks = [
                    {"result_name": "max_stress_mises",},
                    {"result_name": "mises_field", "output": "file"},
                ]
            )
        ]
    )

In the extraction script, we can use :mod:`hookkit` and :mod:`datkit` to simplify the extraction process. The following example shows how to extract the maximum Mises stress and the Mises stress field from the dat file.

.. code-block:: python

    import hookkit
    import datkit

    def extract_one(dat_path, task):
        name = task['result_name']

        doc = datkit.parse(dat_path)
        tables = datkit.select_tables(doc, kind='element', increment='last')
        header, rows = datkit.to_rows(tables, columns=['MISES'])

        if name == 'max_stress_mises':
            return hookkit.scalar(datkit.reduce_rows(rows, header, 'MISES', 'max'))
        if name == 'mises_field':
            return hookkit.field(task, rows, header)
        raise ValueError("unsupported result_name: %s" % name)

    if __name__ == '__main__':
        hookkit.run(extract_one, source_arg='--dat_path')


``Datkit`` Usage
----------------

``datkit`` is where the capability lives. The parser is generic across Abaqus's printed tables (``N O D E`` / ``E L E M E N T`` / ``E N E R G Y`` / ``C O N T A C T`` output) and streams, so one increment costs the same however long the analysis ran:

.. code-block:: python

    from ABQflow import datkit

    doc = datkit.parse(dat_path, increments="last")   # streams; peak memory is one increment
    tables = datkit.select_tables(doc, kind="node", set_name="*INSPECTION*")
    header, rows = datkit.to_rows(tables, columns=["U2"])

    peak = datkit.reduce_rows(rows, header, "U2", "absmax")   # sign preserved
    where = datkit.summary(tables[-1], "MAXIMUM AT")          # which node peaked
    print(doc["completed"], doc["terminator"])                # how the run ended

.. list-table:: Datkit API
   :header-rows: 1
   :widths: 40 60

   * - Function
     - Purpose
   * - `parse(path, kinds=, set_name=, steps=, increments=, keep_rows=, max_rows=)`
     - Whole file → document dict
   * - `iter_tables(path, ...)`
     - Stream tables, retaining nothing
   * - `scan_status(path)`
     - Read only the tail: did the analysis complete?
   * - `select_increments` / `select_tables`
     - Pick by step, increment, kind, set name (`fnmatch`)
   * - `to_rows(tables, columns=, labels=, index_columns=, ...)`
     - Flatten to `(header, rows)`
   * - `column_values` / `summary`
     - One column; one `MAXIMUM` / `MINIMUM` / `MAXIMUM AT` row
   * - `reduce_values` / `reduce_rows`
     - `last` `first` `max` `min` `absmax` `absmin` `sum` `mean` `count` `range`