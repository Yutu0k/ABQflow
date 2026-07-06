Architecture
============

This page describes the design rationale and internals of ABQflow (v0.3.0).

Design Principles
-----------------

**Data + Service = Strategy**

The core insight of v0.3 is splitting the old ``AbaqusCalculation`` God-object into
two narrow contracts:

* :class:`~ABQflow.JobContext` — **frozen** data: paths, job name, CPU count.
  Strategies see data, not implementation.
* :class:`~ABQflow.AbaqusRunner` — a **service** with three public methods:
  ``run_solver()``, ``run_hook()``, and the internal ``_base_command()``.

Strategies depend only on ``(ctx, runner, logger)`` — not on ``AbaqusCalculation``
private methods.  This eliminates the circular import that required
``TYPE_CHECKING`` hacks, makes strategies independently testable (mock the
runner), and gives each layer a clear responsibility.

.. code-block:: text

   ┌──────────────────────────────────────────────────┐
   │ User Layer                                        │
   │   JobSpec (dataclass, validate + deep-copy)       │
   │   BatchSpec = list[JobSpec]                       │
   └────────────────┬─────────────────────────────────┘
                    │ StrategyRegistry.build(spec)
   ┌────────────────▼─────────────────────────────────┐
   │ Orchestration Layer  BatchAbaqusProcessor          │
   │   plan()      — conflict detection (no side fx)   │
   │   prepare()   — apply decisions, build calcs      │
   │   run_batch() — ProcessPoolExecutor + fault-tol   │
   │   ResourcePlanner — CPU / license constraints     │
   └────────────────┬─────────────────────────────────┘
                    │ JobContext (frozen) + AbaqusRunner
   ┌────────────────▼─────────────────────────────────┐
   │ Execution Layer                                    │
   │   JobContext   — paths, name, resources            │
   │   AbaqusRunner — run_solver / run_hook             │
   │   Strategy     — depends on (ctx, runner, logger)  │
   └──────────────────────────────────────────────────┘

Strategy Pattern
----------------

Workflows are composed from three strategy types:

:class:`~ABQflow.PreparationStrategy`
   Generates an INP file.  Built-in implementations:

   * :class:`~ABQflow.InpModifyStrategy` — replace ``{{placeholders}}``
     in a base INP file.  Validates coverage at prepare-time; missing parameters
     produce a clear error, not a silently broken input file.
   * :class:`~ABQflow.ModelGenerationStrategy` — run a Python script
     (with CAE kernel) that builds the model and exports an INP.
   * *Custom* — register via :func:`~ABQflow.register_preparation`.

:class:`~ABQflow.ExtractionStrategy`
   Extracts data from simulation outputs.  Built-in:

   * :class:`~ABQflow.OdbExtractionStrategy` — post-simulation ODB
     extraction (uses ``odbAccess``, no CAE kernel needed).
   * :class:`~ABQflow.ModelPropertiesExtractionStrategy` — pre-simulation
     INP extraction (uses CAE kernel / ``mdb``).

:class:`~ABQflow.JobWorkflowStrategy`
   Orchestrates the full pipeline:

   * :class:`~ABQflow.ModularWorkflowStrategy` —
     preparation → pre-extraction → simulation → post-extraction.
   * :class:`~ABQflow.MonolithicWorkflowStrategy` —
     single script handles everything; results returned as JSON on stdout.

Execution Environments
----------------------

:class:`~ABQflow.AbaqusRunner` selects the correct Python interpreter
based on what the script needs:

.. list-table::
   :header-rows: 1

   * - Condition
     - Command
     - Use Case
   * - ``abqpy`` installed
     - ``python script.py``
     - Any script (recommended)
   * - Needs CAE kernel (``mdb``)
     - ``abaqus cae noGUI=script.py --``
     - Model generation, INP extraction
   * - Needs odbAccess only
     - ``abaqus python script.py``
     - ODB post-processing

The ``--`` separator after ``noGUI=`` prevents Abaqus from consuming custom arguments.

Resource Planning
-----------------

The framework automatically caps parallelism to avoid oversubscribing CPU cores
or Abaqus license tokens.

**Abaqus license token formula** (official): a job using *n* CPU cores consumes

.. math::

   T(n) = \lceil 5 \cdot n^{0.422} \rceil

Example token counts: 1→5, 2→7, 4→9, 8→12, 16→16.

**Parallelism limits:**

.. math::

   P_{cpu}     &= \lfloor (C - R) / c \rfloor \\
   P_{license} &= \lfloor L / T(c) \rfloor \\
   P_{actual}  &= \max(1, \min(P_{req}, P_{cpu}, P_{license}))

where *C* = physical cores, *R* = reserved cores (default 1), *c* = cores per
job, *L* = available tokens.

Use :func:`~ABQflow.plan_parallelism` to compute this directly.

Fault Tolerance
---------------

``run_batch`` uses :class:`concurrent.futures.ProcessPoolExecutor` with these
guarantees:

* **Single-job isolation**: an exception in one worker returns as an error
  :class:`~ABQflow.JobOutcome` — it does not kill the batch.
* **Clean process lifecycle**: the executor context-manager guarantees worker
  cleanup on completion or error.
* **Pickle-safe workers**: the top-level ``_worker`` function (not a lambda or
  closure) is the entry point, ensuring Windows ``spawn`` compatibility.

JSON Protocol
-------------

Hook scripts and monolithic scripts communicate results via stdout.  The
framework uses a **sentinel-marker** approach to reliably extract JSON even
when Abaqus prints banner text or warnings to stdout.

.. code-block:: python

   import json, sys

   results = {"max_stress": 4525.3, "status": "COMPLETED"}
   sys.__stdout__.write("===ABQ_RESULT_BEGIN===\n")
   sys.__stdout__.write(json.dumps(results) + "\n")
   sys.__stdout__.write("===ABQ_RESULT_END===\n")

When sentinel markers are absent, the framework falls back to scanning from the
**end** of stdout for the last complete JSON object (Abaqus banner precedes
script output, so the last ``{`` is most likely the result).

Configuration Validation
------------------------

:class:`~ABQflow.JobSpec` validates at construction time:

* ``workflow='modular'`` requires a ``preparation`` field.
* ``workflow='monolithic'`` requires a ``monolithic_script`` field.
* Unknown workflow values raise ``ValueError`` immediately.

This means misconfiguration surfaces before any Abaqus process is launched —
no silent ``KeyError`` at job initialization.

Migration from v0.2
--------------------

v0.3 introduced breaking changes to fix structural defects (see the
`design document <https://github.com/Yutu0k/ABQflow>`_ for the full
analysis).

**Dict config → JobSpec:**

Old::

   jobs = [{'job_name': 'x', 'type': 'inp_based', 'base_inp_path': '...', 'params': {...}}]

New (compatible — ``from_dict`` bridge)::

   spec = JobSpec.from_dict({'job_name': 'x', 'type': 'inp_based', 'base_inp_path': '...', 'params': {...}})

New (native)::

   spec = JobSpec(job_name='x', preparation=PreparationSpec(kind='inp_based', source_path='...', params={...}))

**Batch result format:**

Old: ``run_batch()`` returned ``list[dict]`` or ``dict[str, dict]``.
New: returns ``list[JobOutcome]``.  Use
:func:`~ABQflow.outcomes_to_list` or
:func:`~ABQflow.outcomes_to_dict` for the old format.

**Strategy signatures:**

Custom strategies that subclasses ``PreparationStrategy`` / ``ExtractionStrategy`` /
``JobWorkflowStrategy`` must change their method signatures from
``(self, context: AbaqusCalculation)`` to
``(self, ctx: JobContext, runner: AbaqusRunner, logger: Logger)``.

**Constructor side-effects:**

``BatchAbaqusProcessor.__init__`` no longer deletes directories or prompts for
input.  Call ``plan()`` / ``prepare()`` explicitly, or let ``run_batch()``
auto-call them.  The default ``duplicate_mode`` is now ``'fail'`` (was
``'interactive'``).
