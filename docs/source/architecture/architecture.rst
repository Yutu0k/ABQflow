:hide-toc:

Architecture
============

Overview
--------

ABQflow has a layered architecture shown in the diagram below. The diagram is generated using `Archify <https://github.com/tt-a1i/archify>`_.

.. raw:: html

   <div class="archify-embed">
     <iframe src="../../_static/abqflow-architecture.html?embed=1"
             title="ABQflow architecture diagram (interactive)"
             loading="lazy"
             allowfullscreen></iframe>
   </div>


Design Principles
-----------------

**Data + Service = Strategy**

Strategies depend only on ``(ctx, runner, logger)`` — never on
``AbaqusCalculation`` internals.  This removes the circular import that used to
require ``TYPE_CHECKING`` hacks, makes strategies independently testable (mock
the runner), and gives each layer one responsibility.

**Every extension point is a registry, not an** ``if``

Preparation kinds and extraction sources resolve through dict lookups
(:data:`~ABQflow.PREPARATION_REGISTRY`, :data:`~ABQflow.EXTRACTION_REGISTRY`),
so adding a workflow means registering a factory rather than editing dispatch
code.

**Fail at construction, not at the solver**

:class:`~ABQflow.JobSpec`, :class:`~ABQflow.HookSpec`,
:class:`~ABQflow.SubroutineSpec` and the registry's option checking all validate
eagerly.  A misconfigured batch raises before any Abaqus process — and any
license token — is spent.

Layers
------

.. list-table::
   :header-rows: 1
   :widths: 22 30 48

   * - Layer
     - Modules
     - Responsibility
   * - Declaration
     - ``core/spec.py``, ``helpers/convert.py``
     - Typed job configuration, validated in ``__post_init__``.
   * - Orchestration
     - ``core/abaqus_automation.py``, ``core/hosts.py``, ``core/status.py``
     - Conflict planning, host assignment, concurrency, fault isolation,
       lifecycle state.
   * - Assembly
     - ``core/registry.py``, ``core/strategies.py``
     - Turn a spec into a concrete strategy chain.
   * - Execution gateway
     - ``core/context.py``, ``core/runner.py``, ``core/backends/``
     - Decide the command, the interpreter, and the machine it runs on.
   * - Job directory
     - ``hookkit.py``, ``datkit.py``, ``core/diagnostics.py``
     - The Abaqus-side contract, and the verdict read back off disk.

Strategy Pattern
----------------

Workflows are composed from three strategy types:

:class:`~ABQflow.PreparationStrategy`
   Generates an INP file.  Built-in implementations:

   * :class:`~ABQflow.InpModifyStrategy` (``kind='inp_based'``) — replace
     ``{{placeholders}}`` in a base INP file.  Validates coverage at
     prepare-time; missing parameters produce a clear error, not a silently
     broken input file.
   * :class:`~ABQflow.ExistingInpStrategy` (``kind='existing_inp'``) — the same
     pipeline with an empty parameter set plus an assertion that none was
     needed, for a batch of already-written decks.  A leftover
     ``{{placeholder}}`` is reported as "you handed a template to a batch of
     finished decks", not as a missing parameter.
   * :class:`~ABQflow.ModelGenerationStrategy` (``kind='model_generation'``) —
     run a Python script (with CAE kernel) that builds the model and exports an
     INP.
   * *Custom* — register via :func:`~ABQflow.register_preparation`.

   Both INP kinds walk the ``*INCLUDE`` tree (:mod:`ABQflow.core.inp_include`).
   ``include_staging='reference'`` (the default) points the deck at the absolute
   paths of *static* includes, so a shared mesh is never copied per job.
   Parameterized includes have no such choice — their content exists nowhere on
   disk, so they are always written into the job directory.

:class:`~ABQflow.ExtractionStrategy`
   Extracts data from simulation outputs.  Built-in:

   * :class:`~ABQflow.OdbExtractionStrategy` — post-simulation ODB
     extraction (uses ``odbAccess``, no CAE kernel needed).
   * :class:`~ABQflow.DatExtractionStrategy` — post-simulation extraction
     from the printed ``.dat`` output, for values written by ``*NODE PRINT``
     / ``*EL PRINT`` when the ODB is too large to open.  Plain text, so it
     runs under the host Python and consumes no license token.
   * :class:`~ABQflow.ModelPropertiesExtractionStrategy` — pre-simulation
     INP extraction (uses CAE kernel / ``mdb``).

   Which one a post-extraction hook gets is decided by its
   :attr:`~ABQflow.HookSpec.source` (``'odb'`` by default, or ``'dat'``);
   :func:`~ABQflow.build_workflow` groups consecutive hooks that share a
   source, so declaration order survives a mixed list.  Add another artifact
   via :func:`~ABQflow.register_extraction`.

:class:`~ABQflow.JobWorkflowStrategy`
   Orchestrates the full pipeline:

   * :class:`~ABQflow.ModularWorkflowStrategy` — preparation → (subroutine
     compile) → pre-extraction → (preflight) → simulation → post-extraction.
   * :class:`~ABQflow.MonolithicWorkflowStrategy` — single script handles
     everything; results returned as JSON on stdout.

   :class:`~ABQflow.SubroutineCompileStrategy` is inserted only when the spec
   carries a :class:`~ABQflow.SubroutineSpec`, and skipped entirely when that
   spec sets ``precompiled=True``.  ``preflight='syntaxcheck'`` or
   ``'datacheck'`` inserts a cheap solver pass before the real run, so a broken
   deck fails in seconds instead of after an hour of queueing.

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
   * - ``interpreter='host'``
     - ``<sys.executable> script.py``
     - ``.dat`` post-processing — plain text, no solver, no license token

The ``--`` separator after ``noGUI=`` prevents Abaqus from consuming custom arguments.

``interpreter='host'`` outranks the rows above it: it describes the *artifact*
rather than the environment, so no Abaqus entry point applies however this
machine is configured.  A host hook also runs on **this** machine even when the
backend is remote — :class:`~ABQflow.DatExtractionStrategy` fetches the
``.dat`` home first, nothing is uploaded, artifact paths are not remapped, and
sidecar CSVs are written straight into the local job directory.  Set
``ABQFLOW_HOST_PYTHON`` to override the interpreter when ``sys.executable`` is
a frozen or embedded binary.

Execution Backends
------------------

:class:`~ABQflow.ExecutionBackend` is the seam between *what* command to run and
*where* it runs.  The runner builds one command line; the backend decides whose
CPU executes it.

* :class:`~ABQflow.LocalBackend` — reproduces plain ``subprocess`` behaviour
  exactly, and is the default everywhere.  Remote execution is strictly opt-in.
* ``SSHBackend`` — uploads the job directory, runs the command on the remote
  machine, fetches the small text artifacts back.  Selected by giving a
  :class:`~ABQflow.HostSpec` a ``hostname``; requires the ``remote`` extra.
* :class:`~ABQflow.RecordingBackend` — records commands instead of running them,
  which is what makes a dry run (and most of the test suite) possible.

Two design decisions are worth stating explicitly.

**There is no filesystem abstraction.**  After a remote solve, the small text
artifacts (``.sta`` / ``.msg`` / ``.dat``, kilobytes to megabytes) are copied
back into the job's *local* directory and the existing
:func:`~ABQflow.diagnose` runs against them unchanged.  The multi-gigabyte
``.odb`` stays where the solver wrote it.  That keeps ``diagnostics.py`` — the
most carefully tested module in the package — at zero changes, and leaves the
local job directory a complete, reproducible artifact.

**Paths are remapped, commands are not.**
:meth:`~ABQflow.ExecutionBackend.map_context` rewrites a
:class:`~ABQflow.JobContext` into the remote directory layout, so strategies
build the same command line for every backend.

Long solver runs use :meth:`~ABQflow.ExecutionBackend.submit_detached` rather
than a held-open channel: the command is launched detached and its exit code
lands in a ``<job>.abqflow.rc`` sentinel file, which the poller stats.  A
dropped SSH connection therefore cannot orphan a running solve or report it as
a failure.  Stale ``.abqflow.rc`` / ``.abqflow.out`` / ``.lck`` files are
cleared before a re-run, so the previous attempt's verdict is never mistaken
for this one's.

Multi-Machine Scheduling
------------------------

A batch with no :class:`~ABQflow.HostSpec` runs locally, exactly as it always
has.  Supplying hosts is the only way to reach the pooled code path.  Two
independent knobs control the behaviour, and conflating them is the mistake
:mod:`ABQflow.core.hosts` exists to prevent:

``max_concurrent``
   How many jobs may run on a machine **at the same time** — a capacity limit,
   enforced by a per-host :class:`threading.Semaphore` during execution.

``weight``
   What **share of the batch** a machine should receive — a throughput
   preference, applied by :func:`~ABQflow.assign_hosts` when jobs are dealt out.

They are not the same thing.  Of two machines measured during development, the
one with half the cores finished the same job 60% faster; ranking purely by core
count would have sent most of the work to the slower machine.  So ``weight``
defaults to capacity (a reasonable proxy) but can be set explicitly once you
know how fast a machine actually is.

:func:`~ABQflow.assign_hosts` deals jobs out one at a time to whichever machine
is currently *least loaded relative to its own weight*.  With a single host this
is the identity assignment — which is what lets the remote path be adopted
without changing single-machine behaviour.

Where an unset ``max_concurrent`` leaves capacity to be derived,
:meth:`HostSpec.capacity <ABQflow.HostSpec.capacity>` takes the minimum of a
core-derived figure and a token-derived one; an unset ``cpus_total`` is
*measured* (read locally, probed remotely) rather than assumed, so a host is
never starved down to a capacity of 1 beside its peers.

**Threads, not processes, for pooled batches.**  Remote work is upload → launch
→ sleep → stat → download, and local work is ``Popen.wait()``; both release the
GIL for their whole duration, so the solver processes run in parallel
regardless.  Processes would buy nothing here, would make SSH connection reuse
impossible, and cannot carry a paramiko client across the boundary at all —
while the semaphores that enforce per-host limits only work inside one process.
A host-less batch keeps :class:`~concurrent.futures.ProcessPoolExecutor`,
unchanged.

Fault Tolerance
---------------

``run_batch`` gives these guarantees:

* **Single-job isolation**: an exception in one worker returns as an error
  :class:`~ABQflow.JobOutcome` — it does not kill the batch.
* **Clean process lifecycle**: the executor context-manager guarantees worker
  cleanup on completion or error.
* **Pickle-safe workers**: the top-level ``_worker`` function (not a lambda or
  closure) is the entry point, ensuring Windows ``spawn`` compatibility.
* **No silent disappearances**: futures are keyed by the calculation rather than
  by its name, so a worker that dies without returning still yields an outcome
  carrying a real error and a usable ``output_dir``.

The three-phase lifecycle keeps side effects where you can see them:
:meth:`~ABQflow.BatchAbaqusProcessor.plan` is pure computation over directory
conflicts, :meth:`~ABQflow.BatchAbaqusProcessor.prepare` applies the decisions
(``'fail'`` by default; also ``'skip'``, ``'overwrite'``, ``'interactive'``),
and :meth:`~ABQflow.BatchAbaqusProcessor.run_batch` executes.  Nothing is
deleted by a constructor.

Status and Diagnostics
----------------------

:class:`~ABQflow.JobStatusManager` moves each job through a state machine —
``CREATED`` → ``PREPARING`` → ``SIMULATING`` → ``EXTRACTING`` → ``COMPLETED`` —
with terminal-state protection: once a job reaches a failure state
(``PREPARATION_FAILED``, ``PREFLIGHT_FAILED``, ``SIMULATION_FAILED``,
``EXTRACTION_FAILED``, ``SUBROUTINE_COMPILE_FAILED``, ``JSON_DECODE_ERROR``,
``SCRIPT_ERROR``, ``UNKNOWN_ERROR``), no further transition is allowed.  The
failure state names the phase, so a batch log says *where* a job died, not just
that it did.

The verdict itself does not come from the exit code alone.
:func:`~ABQflow.parse_sta` reads the ``.sta`` file's completion marker,
:func:`~ABQflow.harvest_errors` pulls deduplicated ERROR lines out of
``.msg`` / ``.dat``, and :func:`~ABQflow.apply_truth_table` cross-references the
two against the subprocess return code.  A zero exit code with no ``COMPLETED``
marker in the ``.sta`` is a failure — Abaqus is perfectly capable of returning 0
after aborting an analysis.

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

``ABQflow.hookkit`` (staged into the job's working directory automatically)
implements this protocol for hook scripts so authors never write sentinel
markers or argparse plumbing by hand.  It is single-file and stdlib-only —
never imports ``ABQflow``, ``odbAccess``, ``abaqus``, or ``numpy`` — so it
runs unmodified under the Abaqus Python interpreter (Py2.7 or Py3).  It also
adds a field-output mode (``hookkit.field()``) that spills large result sets
(>10k rows or >1MB) to a CSV sidecar instead of inlining them in the JSON
payload, keeping stdout small for bulky field quantities.

``ABQflow.datkit`` is staged the same way, but only for hooks with
``source='dat'``, and carries the same contract — single file, stdlib only,
Py2.7 and Py3, never imports ``ABQflow``.  It parses Abaqus's printed tables
(``N O D E`` / ``E L E M E N T`` / ``E N E R G Y`` / ``C O N T A C T``
output) into rows and columns, streaming so that ``parse(path,
increments='last')`` costs one increment however long the analysis ran.
``test/unit/test_hookkit_py27.py`` enforces the Python 2.7 promise on both
files with an AST scan.

The ``abqflow-check-hook`` command runs a hook exactly the way
:meth:`~ABQflow.AbaqusRunner.run_hook` would — same staging, same command line,
same envelope validation — and grades the output against the contract the
framework will later hold it to.  A hook that passes there will run inside
ABQflow; one that fails would have failed inside ABQflow with far less to go on.

Extension Points
----------------

None of the following requires editing framework code:

.. list-table::
   :header-rows: 1
   :widths: 34 66

   * - To add
     - Use
   * - A preparation kind
     - :func:`~ABQflow.register_preparation` (populates
       :data:`~ABQflow.PREPARATION_REGISTRY`)
   * - An extraction source
     - :func:`~ABQflow.register_extraction` (populates
       :data:`~ABQflow.EXTRACTION_REGISTRY`)
   * - A custom strategy
     - Subclass :class:`~ABQflow.PreparationStrategy` /
       :class:`~ABQflow.ExtractionStrategy` /
       :class:`~ABQflow.JobWorkflowStrategy` with the
       ``(ctx, runner, logger)`` signature
   * - An execution target
     - Subclass :class:`~ABQflow.ExecutionBackend` and return it from
       :func:`~ABQflow.make_backend`
to override the interpreter when ``sys.executable`` is
a frozen or embedded binary.

