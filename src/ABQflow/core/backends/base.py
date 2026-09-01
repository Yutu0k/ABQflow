"""ExecutionBackend — the seam between *what* command to run and *where* it runs.

:class:`~ABQflow.core.backends.local.LocalBackend` reproduces today's
``subprocess`` behaviour exactly and is the default everywhere, so remote
execution is strictly opt-in and existing callers see no change.

The design decision worth stating up front: **there is no filesystem
abstraction.**  After a remote solve, the small text artifacts (``.sta`` /
``.msg`` / ``.dat``, kilobytes to megabytes) are copied back into the job's
*local* directory and the existing :func:`~ABQflow.core.diagnostics.diagnose`
runs against them unchanged.  The multi-gigabyte ``.odb`` stays where the
solver wrote it.  That keeps ``diagnostics.py`` — the most carefully tested
module in the package — at zero changes, and leaves the local job directory a
complete, reproducible artifact.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..context import JobContext


@dataclass
class ExecResult:
	"""Outcome of one synchronous command execution.

	Deliberately shaped like :class:`subprocess.CompletedProcess` — same
	three attribute names — so call sites in
	:class:`~ABQflow.core.runner.AbaqusRunner` need no restructuring.

	Attributes
	----------
	returncode : int or None
		Exit code; ``None`` when the command timed out or never launched.
	stdout, stderr : str
		Captured output.
	"""

	returncode: int | None
	stdout: str = ''
	stderr: str = ''

	@property
	def ok(self) -> bool:
		return self.returncode == 0


@dataclass
class JobHandle:
	"""Reference to a detached solver process.

	Plain data only, so a poll loop can be resumed after a dropped
	connection: every completion signal lives in a file on the executing
	machine, never in this object.

	Attributes
	----------
	job_name : str
		Abaqus job name.
	work_dir : str
		Directory on the *executing* machine holding the job's files.
	pid : int or None
		Process id on the executing machine, used by the terminate
		escalation ladder.  ``None`` when the launcher could not report one,
		in which case the ladder skips its force-kill rung.
	method : str
		How the process was launched, for diagnostics.
	launch_rc : int or None
		Return code of the launch call itself (not of the solver).
	launch_output : str
		Raw launcher output, kept for error messages.
	"""

	job_name: str
	work_dir: str
	pid: int | None = None
	method: str = 'local'
	launch_rc: int | None = 0
	launch_output: str = ''

	@property
	def rc_path(self) -> str:
		"""Sentinel written only *after* the solver exits — the authoritative
		"this job has finished" signal.  ``.lck`` cannot play that role: it
		does not exist for the first few seconds after launch."""
		return f'{self.work_dir}\\{self.job_name}.abqflow.rc'

	@property
	def lck_path(self) -> str:
		return f'{self.work_dir}\\{self.job_name}.lck'

	@property
	def out_path(self) -> str:
		return f'{self.work_dir}\\{self.job_name}.abqflow.out'


class ExecutionBackend(ABC):
	"""Runs commands and moves files on behalf of :class:`AbaqusRunner`.

	Implementations must satisfy the shared conformance suite in
	``test/unit/test_backend_conformance.py``: ``LocalBackend`` is the
	executable specification, and every other backend is asserted against
	the identical checks, so a backend that diverges from local semantics
	fails there rather than six hours into a solve.
	"""

	#: Short identifier, used in per-host cache-marker filenames.
	name: str = 'local'

	#: Whether commands run against a directory tree that needs inputs staged
	#: into it and results fetched back.  True for any remote backend, and
	#: also for a :class:`LocalBackend` given an explicit ``work_root``.
	is_remote: bool = False

	# ---- context mapping ----

	@abstractmethod
	def map_context(self, ctx: JobContext) -> JobContext:
		"""Return the context as seen *by the machine that runs the commands*.

		:class:`LocalBackend` returns *ctx* unchanged.  A remote backend
		returns ``dataclasses.replace(ctx, output_dir=<remote job dir>,
		abaqus_exe=<host.abaqus_exe>)`` so every derived path property
		(``inp_path`` / ``odb_path`` / ``sta_path`` / …) recomputes against
		the remote work root — which is why the pure command builders
		(:meth:`AbaqusRunner.build_solver_command` and friends) need no
		changes at all, and :class:`JobContext` stays frozen and local-first.
		"""

	# ---- synchronous execution ----

	@abstractmethod
	def run(self, cmd: list[str], cwd: str, timeout: float | None = None) -> ExecResult:
		"""Run *cmd* to completion and capture its output.

		Must never raise for an ordinary command failure — a non-zero exit
		is data, returned in :attr:`ExecResult.returncode`.
		"""

	# ---- detached execution + polling (solver only) ----

	@abstractmethod
	def submit_detached(self, cmd: list[str], cwd: str, job_name: str,
						timeout: float | None = None) -> JobHandle:
		"""Start the solver so it outlives this connection, and return a handle."""

	@abstractmethod
	def poll(self, handle: JobHandle) -> int | None:
		"""Return the solver's return code, or ``None`` while it is still running."""

	@abstractmethod
	def terminate(self, handle: JobHandle, abaqus_exe: str, grace_s: int) -> list[str]:
		"""Run the terminate escalation ladder; return a log of what each rung did."""

	def wait(self, handle: JobHandle, timeout_s: float | None = None,
			interval: float = 2.0, max_interval: float = 30.0
			) -> tuple[str, int | None, float]:
		"""Block until *handle* finishes or *timeout_s* elapses.

		The default polls :meth:`poll` with exponential backoff, which is what
		a remote backend needs — the answer lives in a file on another
		machine.  :class:`LocalBackend` overrides this to wait on the real
		process handle instead, so running locally keeps its immediate
		wake-up rather than inheriting up to *max_interval* of poll latency.

		Returns
		-------
		tuple[str, int | None, float]
			``(verdict, returncode, elapsed)`` with *verdict* one of
			``'finished'`` / ``'timeout'``.
		"""
		import time

		start = time.time()
		wait_s = interval
		while True:
			rc = self.poll(handle)
			elapsed = time.time() - start
			if rc is not None:
				return 'finished', rc, elapsed
			if timeout_s is not None and elapsed >= timeout_s:
				return 'timeout', None, elapsed
			time.sleep(wait_s)
			wait_s = min(wait_s * 1.5, max_interval)

	# ---- filesystem on the executing machine ----

	@abstractmethod
	def exists(self, path: str) -> bool: ...

	@abstractmethod
	def makedirs(self, path: str) -> None: ...

	@abstractmethod
	def put(self, local_path: str, remote_path: str) -> int:
		"""Upload one file; return bytes transferred."""

	@abstractmethod
	def put_text(self, text: str, remote_path: str) -> int:
		"""Write *text* directly to a file on the executing machine (CRLF)."""

	@abstractmethod
	def get(self, remote_path: str, local_path: str) -> bool:
		"""Download one file; ``False`` when the source does not exist."""

	@abstractmethod
	def glob_get(self, remote_dir: str, patterns: tuple[str, ...],
				local_dir: str) -> list[str]:
		"""Download every file in *remote_dir* matching *patterns*."""

	@abstractmethod
	def remove(self, path: str) -> bool: ...

	@abstractmethod
	def close(self) -> None:
		"""Release connections.  Must be safe to call more than once."""

	# ---- convenience shared by every backend ----

	def read_text(self, path: str, max_bytes: int = 65536) -> str | None:
		"""Read a small file on the executing machine, or ``None`` if absent."""
		raise NotImplementedError

	def clear_sentinels(self, work_dir: str, job_name: str) -> None:
		"""Delete completion markers left by a previous run of *job_name*.

		Re-running a job into a directory that still holds ``.abqflow.rc``
		makes the poll loop report "finished" before the solver has started,
		and then read the *previous* run's results.  Every launch clears
		these first.
		"""
		for ext in ('.abqflow.rc', '.abqflow.out', '.lck'):
			self.remove(f'{work_dir}\\{job_name}{ext}')

	def __enter__(self) -> ExecutionBackend:
		return self

	def __exit__(self, *exc) -> bool:
		self.close()
		return False
