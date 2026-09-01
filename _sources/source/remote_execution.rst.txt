Remote Execution
================

A batch can run on other machines instead of — or alongside — the one you
submit from.  Everything is opt-in: without a ``hosts`` argument the code path
is unreachable and behaviour is exactly what it has always been.

.. code-block:: python

   import os
   from ABQflow import BatchAbaqusProcessor, HostSpec

   hosts = [
       HostSpec.local(name='laptop', max_concurrent=1),
       HostSpec(
           name='node01',
           hostname='NODE01',                        # bare IPv6/IPv4 also works
           username='abaquser',
           password=os.environ['NODE01_PASSWORD'],   # never hard-code this
           abaqus_exe=r'C:\SIMULIA\Commands\abaqus.bat',
           work_root=r'D:\abqwork',
           cpus_total=32,
           max_concurrent=2,
       ),
   ]

   processor = BatchAbaqusProcessor(
       batch_data=specs,
       base_output_dir=OUTPUT_DIR,
       cpus_per_job=2,
       abaqus_exe=LOCAL_ABAQUS,
       hosts=hosts,          # the only line that makes this multi-machine
   )
   outcomes = processor.run_batch(num_parallel_jobs=4)

Requires the optional dependency::

   pip install "ABQflow[remote]"

``paramiko`` is imported lazily, so a local-only installation never needs it.

Division of Labour
------------------

Preparation always runs **locally**.  INPs are generated on the submitting
machine and then shipped; a remote machine is only ever asked to solve and to
extract.  This keeps model generation reproducible in one place and means a
remote host needs nothing but Abaqus and an SSH server.

What crosses the wire, per job:

.. list-table::
   :header-rows: 1
   :widths: 20 80

   * - Direction
     - Contents
   * - Up
     - the job's ``.inp``, ``*INCLUDE`` targets (see below), ``hookkit.py``,
       hook scripts, the tasks JSON, and a user subroutine if configured
   * - Down
     - ``.sta`` / ``.msg`` / ``.dat`` / ``.log``, sidecar ``.csv`` files, and
       the return-code sentinel — kilobytes to megabytes
   * - Never
     - the ``.odb``, unless ``fetch_odb=True``

Because only small text artifacts come back, ``diagnose()`` runs unchanged
against the local copies and the multi-gigabyte ODB stays where the solver
wrote it.

Distributing Work
-----------------

Two knobs control multi-machine behaviour, and they are deliberately separate.

``max_concurrent``
   How many jobs run on a machine **at once**.  Enforced during execution by a
   per-host semaphore, so a batch can send two concurrent jobs to one machine
   and one to another.

``weight``
   What **share of the batch** a machine receives.  Applied when jobs are
   assigned.  Defaults to the machine's concurrency capacity.

Conflating the two is tempting and wrong.  Of two machines measured during
development, the one with *half* the cores finished the same job 60% faster —
ranking purely by core count would have sent most of the work to the slower
machine.  Set ``weight`` explicitly once you know how fast a machine actually
is:

.. code-block:: python

   HostSpec(name='fast-but-small', cpus_total=16,
            max_concurrent=1,    # one job at a time here
            weight=3.0,          # but give it three times the share
            ...)

When ``max_concurrent`` is not given it is derived from ``cpus_total``,
``cpus_per_job`` and ``license_tokens``, with tokens a hard cap and cores
advisory — overcommitting cores merely slows a machine down, whereas running
out of tokens makes jobs fail to start with errors that look like solver
crashes.

Inspect the plan before running anything:

.. code-block:: python

   >>> processor.assignment()
   {'laptop': ['job_0001'], 'node01': ['job_0002', 'job_0003', 'job_0004']}

.. _shared-include-dir:

``*INCLUDE`` and the Shared Directory
-------------------------------------

Decks that pull in an external mesh or geometry with ``*INCLUDE, INPUT=`` need
those files on the executing machine.  Two things would go wrong naively:

* the directive points at a **local absolute path**, which cannot resolve on
  another machine — the job fails with an opaque Abaqus preprocessing error;
* copying the target into every job directory would make **transfer, not
  solving, the dominant cost** of a sweep, since a referenced mesh is often
  orders of magnitude larger than the deck referencing it.

So include targets are uploaded **once per machine** into a shared directory
beside the job directories::

   <work_root>\
       _abqflow_shared\
           9e13c038540f_mesh.inp        <- uploaded once, reused by every job
       job_0001\
           job_0001.inp                 <- *INCLUDE rewritten to the path above
       job_0002\
           job_0002.inp
       ...

and the directive is rewritten to that absolute remote path.

Names are **content-addressed** — ``<sha256[:12]>_<basename>`` — which buys
three properties from one decision:

* the same file is uploaded once no matter how many jobs or batches reference
  it;
* two different files that happen to share a basename cannot collide;
* "already present remotely" is *exactly* equivalent to "identical content",
  so the existence check is a correct cache check rather than a guess.

Hashing reads the file once from local disk, which is cheap next to sending it
over SFTP.

The shared directory is a cache and is **not** touched by ``cleanup``, which
only ever removes job directories.  Delete it by hand to force a re-upload.

Surviving Disconnects
---------------------

Holding an SSH channel open for the duration of a solve makes the job as
fragile as the connection: a laptop sleeping, a Wi-Fi roam, a VPN reconnect or
sshd's ``ClientAliveInterval`` closes the channel, and Windows OpenSSH
terminates the session's process tree with it.  A six-hour job dies at hour
four.

Instead the solver is launched **detached**, via ``Win32_Process.Create``, so
the process is re-parented away from the sshd session and survives the channel
closing.  Progress is read from files on the remote disk, which makes the poll
loop stateless — it can lose the connection, reconnect, and resume:

.. list-table::
   :header-rows: 1
   :widths: 30 70

   * - Signal
     - Meaning
   * - ``<job>.abqflow.rc``
     - Written by the launcher only *after* the solver exits.  Its existence
       is the authoritative "finished" signal; its contents are the return
       code.
   * - ``<job>.lck``
     - Solver holds the database.  A liveness hint only — Abaqus does not
       create it for the first few seconds, so its *absence* never means
       "done".
   * - ``<job>.sta``
     - Progress, and the ``COMPLETED`` verdict that remains the only success
       certificate.

On timeout the same escalation ladder as local execution runs remotely:
``abaqus terminate`` → grace period → ``taskkill /T /F /PID`` → remove the
``.lck``.

Remote Machine Setup
--------------------

On the target, in an elevated PowerShell:

.. code-block:: powershell

   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
   Start-Service sshd
   Set-Service -Name sshd -StartupType Automatic
   Get-NetFirewallRule -Name *OpenSSH* | Select Name,Enabled,Profile
   New-Item -ItemType Directory -Force -Path D:\abqwork

Points that cost real debugging time:

* **Always give an absolute** ``abaqus_exe``.  A non-interactive SSH session
  inherits machine- and user-level environment but not what a login shell
  profile adds, and installers often put Abaqus on the installing user's
  ``PATH`` only.  ``HostSpec`` refuses a remote host without one.
* **IPv6 literals go in bare** — ``2001:db8::1``, not ``[2001:db8::1]``.  A
  link-local address needs a *numeric* Windows zone index (``fe80::1%12``),
  not an interface name.
* **License environment must be machine-scoped.**  ``ABAQUSLM_LICENSE_FILE``
  set at user scope works over RDP and fails over SSH.
* **Prefer a dedicated local account.**  SSH password authentication against a
  Microsoft-account-backed login is unreliable.
* **Abaqus 2022 and earlier run hooks under Python 2.7.**  ``hookkit.py``
  supports it, but your own hook scripts must too.

Remote Desktop is unaffected: SSH sessions do not occupy an interactive
session slot, and detached jobs run outside any RDP session — they survive you
logging out, and are invisible in Task Manager unless you enable *show
processes from all users*.  They do compete for the same cores and license
tokens as anything you run interactively.

Credentials
-----------

Never hard-code a password in a script or notebook.  Read it from the
environment:

.. code-block:: python

   password=os.environ['NODE01_PASSWORD']

or pass ``key_filename`` and use key authentication instead.

.. warning::

   A notebook stores **cell outputs** as well as source.  Printing a password
   to check it loaded leaves it in the ``.ipynb`` even after the source is
   cleaned — and committing that leaks it.  Clear the outputs of any cell that
   touched a credential before committing.

Dry Runs
--------

``RecordingBackend`` exercises the whole remote pipeline — staging, launcher
generation, poll loop, result fetch — without a network, and is useful for
checking what a batch *would* do:

.. code-block:: python

   from ABQflow import RecordingBackend

   backend = RecordingBackend(work_root=r'D:\abqwork')
   # ... drive an AbaqusRunner with it, then inspect:
   backend.commands('solver')
