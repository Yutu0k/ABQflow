"""Submit a batch of local INP files to one or more remote Windows machines.

Everything remote is opt-in: without ``hosts=``, :class:`BatchAbaqusProcessor`
behaves exactly as it always has and runs on this machine.

Two knobs control how work is spread, and they are deliberately separate:

``max_concurrent``
    How many jobs run on a machine **at the same time**.  This is what lets a
    batch send two concurrent jobs to one machine and one to another.

``weight``
    What **share of the batch** a machine receives.  Omit it and the machine's
    concurrency capacity is used instead.  Set it when a machine is faster per
    job than its core count suggests — of the two machines this example was
    developed against, the one with half the cores finished the same job 60%
    faster, so ranking purely by cores would have sent most of the work to the
    slower one.

Prerequisites on each target machine
------------------------------------
1. OpenSSH Server installed and running::

	   Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
	   Start-Service sshd; Set-Service -Name sshd -StartupType Automatic

2. An inbound firewall rule for port 22 enabled on the *active* network
   profile.
3. The **absolute** path to ``abaqus.bat``.  A bare ``'abaqus'`` usually
   fails: a non-interactive SSH session inherits machine- and user-level
   environment but not what a login shell profile adds, and installers often
   put Abaqus on the installing user's PATH only.
4. Abaqus licence configuration at **machine** scope, not user scope, or the
   solver works over RDP and fails over SSH.

Running this does not interfere with using Remote Desktop on the same
machines: SSH sessions do not occupy an interactive session slot, and jobs
launched here run detached in session 0 so they survive both an SSH
disconnect and an RDP logoff.  They do compete for CPU and licence tokens,
which is what ``reserve_cores`` is for.

Run: pixi run python examples/08_RemoteSubmission/run_remote_batch.py
"""

from __future__ import annotations

import os

from ABQflow import BatchAbaqusProcessor, HostSpec, generate_from_inp_files
from ABQflow.core.spec import JobSpec, PreparationSpec

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
OUTPUT_DIR = os.path.join(HERE, 'output')

# ---------------------------------------------------------------------------
# 1. Describe the machines.
#
# Passwords come from the environment so nothing sensitive lands in the file.
# ---------------------------------------------------------------------------
HOSTS = [
	HostSpec(
		name='node01',
		hostname='NODE01',                 # bare IPv6/IPv4 literal also works,
		username='abaquser',               # with no square brackets
		password=os.environ.get('ABQFLOW_NODE01_PASSWORD', ''),
		abaqus_exe=r'C:\Program Files\SIMULIA\Commands\abaqus.bat',
		work_root=r'D:\abqwork',
		cpus_total=32,
		max_concurrent=2,                  # two jobs at a time on this machine
	),
	HostSpec(
		name='node02',
		hostname='NODE02',
		username='abaquser',
		password=os.environ.get('ABQFLOW_NODE02_PASSWORD', ''),
		abaqus_exe=r'C:\SIMULIA\Commands\abaqus.bat',
		work_root=r'D:\abqwork',
		cpus_total=16,
		max_concurrent=1,                  # but only one at a time here
		weight=3.0,                        # while still taking a large share
	),
]


def build_specs() -> list[JobSpec]:
	"""One job per finished INP found under ``inp_files/``.

	``generate_from_inp_files`` accepts a glob and handles job-name
	sanitisation and natural sorting; the manual fallback below shows the
	same thing spelled out.
	"""
	pattern = os.path.join(HERE, 'inp_files', '*.inp')
	base = JobSpec(job_name='placeholder',
				preparation=PreparationSpec(kind='existing_inp', source_path=''))
	specs = generate_from_inp_files(pattern, base)
	if not specs:
		raise SystemExit(
			f"No .inp files found under {os.path.join(HERE, 'inp_files')} — "
			"put some finished decks there first."
		)
	return specs


def main() -> int:
	missing = [h.name for h in HOSTS if not h.password]
	if missing:
		raise SystemExit(
			"Set a password for: " + ', '.join(missing) + "\n"
			"  $env:ABQFLOW_NODE01_PASSWORD=\"...\""
		)

	specs = build_specs()

	processor = BatchAbaqusProcessor(
		batch_data=specs,
		base_output_dir=OUTPUT_DIR,
		cpus_per_job=2,
		duplicate_mode='overwrite',
		timeout=3600,
		hosts=HOSTS,               # <- the only line that makes this remote
	)

	# How the batch will be spread, before anything runs.
	print("Planned assignment:")
	for host_name, jobs in processor.assignment().items():
		print(f"  {host_name}: {len(jobs)} job(s) — {', '.join(jobs)}")

	outcomes = processor.run_batch(num_parallel_jobs=4)

	print("\nResults:")
	failed = 0
	for oc in sorted(outcomes, key=lambda o: o.job_name):
		mark = 'OK ' if oc.status == 'COMPLETED' else 'FAIL'
		print(f"  [{mark}] {oc.job_name}: {oc.status} ({oc.duration_s or 0:.0f}s)"
			+ (f" — {oc.error}" if oc.error else ''))
		failed += oc.status != 'COMPLETED'

	print(f"\n{len(outcomes) - failed}/{len(outcomes)} completed.")
	print(f"Small artifacts (.sta/.msg/.dat) were fetched into {OUTPUT_DIR};")
	print("the .odb files stayed on the machines that produced them "
		"(set HostSpec.fetch_odb=True to change that).")
	return 1 if failed else 0


if __name__ == '__main__':
	raise SystemExit(main())
