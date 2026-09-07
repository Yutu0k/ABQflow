Extraction Hooks
================


Extraction hooks are a powerful feature in ABQflow that allow users to define custom extraction logic for Abaqus inp/dat/odb results.

.. toctree::
    :maxdepth: 2
    :hidden:

    hooks/odb_extraction_hook
    hooks/dat_extraction_hook
    hooks/inp_extraction_hook

* :doc:`Odb Extraction <hooks/odb_extraction_hook>`: 
    Extract data from Abaqus **ODB** files using custom logic.

* :doc:`Dat Extraction <hooks/dat_extraction_hook>`:
    Extract data from Abaqus **DAT** files using custom logic.

* :doc:`Inp Extraction <hooks/inp_extraction_hook>`:
    Extract data from Abaqus **INP** files using custom logic.

Hook Verification
-----------------

``ABQflow`` provides a convenient way to verify the correctness of your extraction hooks. You can use the :class:`~ABQflow.check_hook` module to check if your hook scripts are functioning as expected.

