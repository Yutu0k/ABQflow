"""LocalBackend — today's behaviour, expressed through the backend protocol.

This is the default for every batch, and the executable specification the
conformance suite holds other backends to.

``put``/``get`` are real copies rather than no-ops on purpose.  Pointing a
``LocalBackend`` at a *different* local directory then exercises the whole
stage-up → solve → stage-down → diagnose pipeline against real files with
real path mapping, no network and no mocks — which is where most staging and
path-joining bugs are actually caught.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import signal
import subprocess
import sys
import time

from ..context import JobContext
from .base import ExecResult, ExecutionBackend, JobHandle


class LocalBackend(ExecutionBackend):
	"""Execute on this machine via :mod:`subprocess`.

	Parameters
	----------
	work_root : str or None
		When given, contexts are remapped to ``<work_root>/<job_name>``
		instead of running in place.  Used by tests to exercise the staging
		path without a network; ``None`` (the default) leaves every context
		untouched, which is what production uses.
	"""

	name = 'local'
	is_remote = False

	def __init__(self, work_root: str | None = None):
		self.work_root = work_root
		# ``is_remote`` means "commands run against a different directory tree,
		# so inputs must be staged there and results fetched back" — not
		# literally "another machine".  A LocalBackend with a work_root needs
		# exactly that, which is what makes it a faithful stand-in for a
		# remote backend in tests.
		self.is_remote = bool(work_root)
		self._procs: dict[str, subprocess.Popen] = {}

	# ---- context mapping ----

	def map_context(self, ctx: JobContext) -> JobContext:
		if not self.work_root:
			return ctx
		from dataclasses import replace
		return replace(ctx, output_dir=os.path.join(self.work_root, ctx.job_name))

	# ---- synchronous execution ----

	def run(self, cmd: list[str], cwd: str, timeout: float | None = None) -> ExecResult:
		try:
			proc = subprocess.run(cmd, cwd=cwd, capture_output=True,
								text=True, timeout=timeout)
			return ExecResult(proc.returncode, proc.stdout or '', proc.stderr or '')
		except subprocess.TimeoutExpired:
			return ExecResult(None, '', f'timeout after {timeout}s')
		except Exception as e:
			return ExecResult(None, '', f'{type(e).__name__}: {e}')

	# ---- detached execution ----

	def submit_detached(self, cmd: list[str], cwd: str, job_name: str,
						timeout: float | None = None) -> JobHandle:
		"""Launch with process-group isolation and keep the live handle.

		Locally there is no reason to detach-and-poll: holding the real
		:class:`~subprocess.Popen` gives the terminate ladder a precise
		process group to reach ``standard.exe`` through, which is strictly
		better than a PID scraped out of WMI.  The rc sentinel is still
		written so both backends expose the same completion signal.
		"""
		self.makedirs(cwd)
		self.clear_sentinels(cwd, job_name)

		popts: dict = {'cwd': cwd,
					'stdout': subprocess.DEVNULL,
					'stderr': subprocess.DEVNULL}
		if sys.platform == 'win32':
			popts['creationflags'] = subprocess.CREATE_NEW_PROCESS_GROUP
		else:
			popts['start_new_session'] = True

		try:
			proc = subprocess.Popen(cmd, **popts)
		except Exception as e:
			return JobHandle(job_name, cwd, None, 'local',
							launch_rc=1, launch_output=f'{type(e).__name__}: {e}')

		self._procs[job_name] = proc
		return JobHandle(job_name, cwd, proc.pid, 'local', launch_rc=0)

	def poll(self, handle: JobHandle) -> int | None:
		proc = self._procs.get(handle.job_name)
		if proc is None:
			# No live handle (resumed session): fall back to the sentinel.
			text = self.read_text(handle.rc_path, 64)
			return _parse_rc(text)
		rc = proc.poll()
		if rc is not None:
			self._write_rc(handle, rc)
		return rc

	def wait(self, handle: JobHandle, timeout_s: float | None = None,
			interval: float = 2.0, max_interval: float = 30.0
			) -> tuple[str, int | None, float]:
		"""Block on the real process handle rather than polling a file.

		Locally we own a live :class:`~subprocess.Popen`, so waiting on it
		returns the instant the solver exits.  Falling back to the base
		class's poll loop would add up to *max_interval* seconds of latency
		per job for no benefit.
		"""
		proc = self._procs.get(handle.job_name)
		if proc is None:
			return super().wait(handle, timeout_s, interval, max_interval)

		start = time.time()
		try:
			proc.wait(timeout=timeout_s)
		except subprocess.TimeoutExpired:
			return 'timeout', None, time.time() - start
		rc = proc.returncode
		self._write_rc(handle, rc)
		return 'finished', rc, time.time() - start

	def _write_rc(self, handle: JobHandle, rc: int):
		"""Record the return code so the completion signal matches remote."""
		try:
			with open(handle.rc_path, 'w', encoding='utf-8') as f:
				f.write(str(rc))
		except OSError:
			pass

	def terminate(self, handle: JobHandle, abaqus_exe: str, grace_s: int) -> list[str]:
		log: list[str] = []
		proc = self._procs.get(handle.job_name)

		res = self.run([abaqus_exe, 'terminate', f'job={handle.job_name}'],
					handle.work_dir, timeout=30)
		log.append(f"level 1 abaqus terminate: rc={res.returncode}")

		if proc is not None:
			try:
				proc.wait(timeout=grace_s)
				log.append("level 2 exited during grace period")
				self._write_rc(handle, proc.returncode)
				return log
			except subprocess.TimeoutExpired:
				log.append(f"level 2 grace {grace_s}s expired")

		if handle.pid is not None:
			log.append(f"level 3 killing process tree of PID {handle.pid}")
			try:
				if sys.platform == 'win32':
					subprocess.run(['taskkill', '/T', '/F', '/PID', str(handle.pid)],
								capture_output=True, timeout=15)
				else:
					os.killpg(handle.pid, signal.SIGKILL)
			except Exception as e:
				log.append(f"level 3 force-kill failed: {e}")
		if proc is not None:
			try:
				proc.wait(timeout=10)
			except subprocess.TimeoutExpired:
				proc.kill()
				proc.wait()

		removed = self.remove(handle.lck_path)
		log.append(f"level 4 remove .lck: {'removed' if removed else 'not present'}")
		return log

	# ---- filesystem ----

	def exists(self, path: str) -> bool:
		return os.path.exists(path)

	def makedirs(self, path: str) -> None:
		if path:
			os.makedirs(path, exist_ok=True)

	def put(self, local_path: str, remote_path: str) -> int:
		if os.path.abspath(local_path) == os.path.abspath(remote_path):
			return os.path.getsize(local_path)
		self.makedirs(os.path.dirname(remote_path))
		shutil.copy2(local_path, remote_path)
		return os.path.getsize(remote_path)

	def put_text(self, text: str, remote_path: str) -> int:
		self.makedirs(os.path.dirname(remote_path))
		data = text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
		with open(remote_path, 'wb') as f:
			f.write(data)
		return len(data)

	def get(self, remote_path: str, local_path: str) -> bool:
		if not os.path.isfile(remote_path):
			return False
		if os.path.abspath(local_path) == os.path.abspath(remote_path):
			return True
		self.makedirs(os.path.dirname(local_path))
		shutil.copy2(remote_path, local_path)
		return True

	def glob_get(self, remote_dir: str, patterns: tuple[str, ...],
				local_dir: str) -> list[str]:
		if not os.path.isdir(remote_dir):
			return []
		fetched = []
		for name in sorted(os.listdir(remote_dir)):
			src = os.path.join(remote_dir, name)
			if not os.path.isfile(src):
				continue
			if not any(fnmatch.fnmatch(name, p) for p in patterns):
				continue
			if self.get(src, os.path.join(local_dir, name)):
				fetched.append(name)
		return fetched

	def remove(self, path: str) -> bool:
		try:
			os.remove(path)
			return True
		except OSError:
			return False

	def read_text(self, path: str, max_bytes: int = 65536) -> str | None:
		try:
			with open(path, 'rb') as f:
				return f.read(max_bytes).decode('utf-8', errors='replace')
		except OSError:
			return None

	def close(self) -> None:
		self._procs.clear()


def _parse_rc(text: str | None) -> int | None:
	"""Extract an integer return code from a sentinel file's contents."""
	if text is None:
		return None
	digits = ''.join(c for c in text if c.isdigit() or c == '-')
	try:
		return int(digits)
	except ValueError:
		return None


def wait_for(backend: ExecutionBackend, handle: JobHandle,
			timeout_s: float | None,
			interval: float = 2.0, max_interval: float = 30.0):
	"""Poll *handle* until it finishes or *timeout_s* elapses.

	Shared by both backends so the completion semantics cannot drift.

	Returns
	-------
	tuple[str, int | None, float]
		``(verdict, returncode, elapsed)`` where *verdict* is ``'finished'``
		or ``'timeout'``.
	"""
	start = time.time()
	wait = interval
	while True:
		rc = backend.poll(handle)
		elapsed = time.time() - start
		if rc is not None:
			return 'finished', rc, elapsed
		if timeout_s is not None and elapsed >= timeout_s:
			return 'timeout', None, elapsed
		time.sleep(wait)
		wait = min(wait * 1.5, max_interval)
