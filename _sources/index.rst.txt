ABQflow Documentation
=====================

**Modular batch-processing framework for Abaqus FEA** -- typed job specs,
strategy-pattern workflows, fault-tolerant parallel execution, resource-aware scheduling.

| |version| | |python| | |license|

.. |version| image:: https://img.shields.io/github/v/release/Yutu0k/ABQflow?label=version
   :alt: Latest release version
.. |python| image:: https://img.shields.io/badge/python-3.9+-blue.svg
   :alt: Python 3.9+
.. |license| image:: https://img.shields.io/badge/license-MIT-green.svg
   :alt: MIT License

----

ABQflow turns repetitive Abaqus FEA workflows into readable, batch-oriented Python
code. Define parameter sweeps, multi-step extraction pipelines, and monolithic scripts
as typed :class:`~ABQflow.JobSpec` objects; the framework handles resource
planning, parallel execution, and fault tolerance.

.. toctree::
   :maxdepth: 1
   :hidden:

   source/getting_started/installation
   source/getting_started/quick_start

.. toctree::
   :maxdepth: 1
   :caption: User Guide
   :hidden:

   source/user_guide/inp_template
   source/user_guide/basic_batch
   source/user_guide/extraction_hook
   source/user_guide/separate
   source/user_guide/diagnostics
   source/user_guide/remote_execution
   source/user_guide/subroutines


.. toctree::
   :maxdepth: 2
   :caption: API Reference
   :hidden:

   source/architecture/architecture
   source/api/ABQflow
