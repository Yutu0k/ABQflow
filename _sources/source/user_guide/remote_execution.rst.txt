Remote Execution
================

Prerequisites
-------------

Local Machine Setup
~~~~~~~~~~~~~~~~~~~

Requires the optional dependency:

.. code-block:: bash

    pip install "ABQflow[remote]"

Remote Machine Setup
~~~~~~~~~~~~~~~~~~~~

.. code-block:: powershell

    Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
    Start-Service sshd
    Set-Service -Name sshd -StartupType Automatic
    Get-NetFirewallRule -Name *OpenSSH* | Select Name,Enabled,Profile
    New-Item -ItemType Directory -Force -Path D:\abqwork

.. note::

    Remote Desktop is **unaffected**: SSH sessions do not occupy an interactive session slot, and detached jobs run outside any RDP session, and are invisible in Task Manager unless you enable *show processes from all users*.


Basic Usage of Remote Execution
-------------------------------

A batch can run on other machines instead of the one you submit from. One can specify a list of remote hosts via :class:`~ABQflow.HostSpec` in :class:`~ABQflow.BatchAbaqusProcessor`. Each host can have its own concurrency limit and weight, which controls how many jobs are sent to it and how many run at once.


.. code-block:: python

    import os
    from ABQflow import BatchAbaqusProcessor, HostSpec

    hosts = [
        HostSpec.local(name='local_machine', max_concurrent=1),
        HostSpec(
            name='node01',
            hostname='NODE01',                        # <- bare IPv6/IPv4 also works
            username='abaquser',
            password=os.environ['NODE01_PASSWORD'],
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
        hosts=hosts,          # <- specify your HOSTS here
    )
    
    outcomes = processor.run_batch(num_parallel_jobs=4)



Division of Labour
------------------

Preparation always runs **locally**. INPs are generated on the submitting machine and then shipped; a remote machine is only ever asked to solve and to extract. This keeps model generation reproducible in one place and means a remote host needs nothing but Abaqus and an SSH server.

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

Distributing Work
-----------------

Multiple knobs control multi-machine behaviour, and they are deliberately separate.

``cpus_per_job``
    The number of cores each job is given. 

``max_concurrent``
    How many jobs run on a machine **at once**.  Enforced during execution by a per-host semaphore, so a batch can send two concurrent jobs to one machine and one to another.

    .. note::

        When ``max_concurrent`` is not given it is derived from ``cpus_total``, ``cpus_per_job`` and ``license_tokens``.

``weight``
    What **share of the batch** a machine receives.  Applied when jobs are assigned.  Defaults to the machine's concurrency capacity.

``cpus_total`` (Optional)
    Total cores on the machine. If leaft unset, it is measured once over SSH before any job is dispatched. Setting it explicitly skips that probe.

.. note::
    If ``cpus_per_job`` * ``max_concurrent`` exceeds ``cpus_total``, a **over-subscription Warning** will be given but preceeding to solver.


Inspect the plan before running anything:

.. code-block:: python

   >>> processor.assignment()
   {'laptop': ['job_0001'], 'node01': ['job_0002', 'job_0003', 'job_0004']}

.. _shared-include-dir:

``*INCLUDE`` and the Shared Directory
-------------------------------------

Decks that pull in with ``*INCLUDE, INPUT=`` need those files on the executing machine. The whole tree is walked through, and each file is placed in one of two tiers.  Which tier is read off the directive's **shape**, which is the convention :mod:`ABQflow.core.inp_include` already established when it resolved the tree locally — so preparation and staging agree with no manifest passed between them:

.. list-table::
   :header-rows: 1
   :widths: 25 30 45

   * - Directive looks like
     - Means
     - Goes to
   * - a bare filename
     - per-job — a template a parameter touched, materialised beside the job's
       own INP
     - the remote **job directory**, name unchanged (Abaqus resolves a bare
       include against the working directory)
   * - anything else
     - static — identical across the batch
     - the machine's **shared directory**, content-addressed

::

   <work_root>\
       _abqflow_shared\
           9e13c038540f_mesh.inp        <- uploaded once, reused by every job
       job_0001\
           job_0001.inp                 <- static *INCLUDE -> the path above
           material.inp                 <- per-job, travels with this job only
       job_0002\
           job_0002.inp
           material.inp                 <- same name, different values
       ...


.. note::
    A per-job file is **not** a candidate for sharing even though it is small.Shared names are **content-addressed** - ``<sha256[:12]>_<basename>``:

    * the same file is uploaded once no matter how many jobs or batches reference it;
    * two different files that happen to share a basename cannot collide;
    * "already present remotely" is *exactly* equivalent to "identical content", so the existence check is a correct cache check rather than a guess.



Surviving Disconnects
---------------------

Holding an SSH channel open for the duration of a solve makes the job as fragile as the connection. Instead the solver is launched **detached** via ``Win32_Process.Create``, so the process is re-parented away from the sshd session and survives the channel closing.  Progress is read from files on the remote disk, which makes the poll loop stateless — it can lose the connection, reconnect, and resume:

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

On timeout the same escalation ladder as local execution runs remotely: ``abaqus terminate`` → grace period → ``taskkill /T /F /PID`` → remove the ``.lck``.


Dry Runs
--------

``RecordingBackend`` exercises the whole remote pipeline — staging, launcher generation, poll loop, result fetch - without a network, and is useful for checking what a batch *would* do:

.. code-block:: python

    from ABQflow import RecordingBackend

    backend = RecordingBackend(work_root=r'D:\abqwork')
    # ... drive an AbaqusRunner with it, then inspect:
    backend.commands('solver')
