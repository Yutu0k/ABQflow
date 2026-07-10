ABQflow
========

**Modular batch-processing framework for Abaqus FEA** -- typed job specs,
strategy-pattern workflows, fault-tolerant parallel execution, resource-aware scheduling.

| |version| | |python| | |license|

.. |version| image:: https://img.shields.io/badge/version-|release|-blue.svg
   :alt: Version |release|
.. |python| image:: https://img.shields.io/badge/python-3.9+-blue.svg
   :alt: Python 3.9+
.. |license| image:: https://img.shields.io/badge/license-MIT-green.svg
   :alt: MIT License

----

ABQflow turns repetitive Abaqus FEA workflows into readable, batch-oriented Python
code. Define parameter sweeps, multi-step extraction pipelines, and monolithic scripts
as typed :class:`~abaqus_batch_pack.JobSpec` objects; the framework handles resource
planning, parallel execution, and fault tolerance.

Quick Links
-----------

* :doc:`Getting Started <source/getting_started>` -- install, first job, batch sweep
* :doc:`Architecture <source/architecture>` -- design overview, strategy pattern, resource planning
* :doc:`API Reference <source/modules>` -- full module reference (auto-generated)

.. toctree::
   :maxdepth: 2
   :hidden:

   source/getting_started
   source/architecture
   source/modules
