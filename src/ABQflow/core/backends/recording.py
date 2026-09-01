"""RecordingBackend — dry-run the remote path with no network and no mocks.

Extends the ``record_only`` idea that :class:`AbaqusRunner` already ships:
commands are recorded instead of executed, and the poll sequence can be
scripted, so the whole remote pipeline — staging, launcher generation, poll
loop, fetch, diagnose — is exercisable in a unit test.

Shipped in the package rather than kept in the test tree, because a remote
dry-run is useful to users for the same reason ``record_only`` is.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import replace

from ...helpers.constant import RESULT_BEGIN, RESULT_END
from ..context import JobContext
from ..runner import CommandRecord
from .base import ExecResult, ExecutionBackend, JobHandle


class RecordingBackend(ExecutionBackend):
	"""Record every command and file operation; execute nothing.

	Parameters
	----------
	work_root : str or None
		When set, contexts are remapped under it, so path mapping is
		exercised exactly as a remote backend would.
	poll_sequence : list[int | None] or None
		Return codes handed out by successive :meth:`poll` calls, e.g.
		``[None, None, 0]`` for "running, running, finished".  Exhausting
		the list yields the last value forever.  ``None`` (the default)
		means every poll reports success immediately.
	name : str
		Identifier reported as the backend name.
	"""

	is_remote = True

	def __init__(self, work_root: str | None = None,
				poll_sequence: list[int | None] | None = None,
				name: str = 'recording',
				hook_results: dict | None = None):
		self.name = name
		self.work_root = work_root
		self.command_log: list[CommandRecord] = []
		self.files: dict[str, bytes] = {}
		self.fetched: list[tuple[str, str]] = []
		self.hook_results = {} if hook_results is None else dict(hook_results)
		self._poll_sequence = list(poll_sequence) if poll_sequence else [0]
		self._poll_index = 0

	# ---- context mapping ----

	def map_context(self, ctx: JobContext) -> JobContext:
		if not self.work_root:
			return ctx
		return replace(ctx, output_dir=os.path.join(self.work_root, ctx.job_name))

	# ---- execution ----

	def run(self, cmd: list[str], cwd: str, timeout: float | None = None) -> ExecResult:
		"""Record *cmd* and answer with a well-formed hook payload.

		Emitting the sentinel block rather than empty output is what lets this
		backend drive :meth:`AbaqusRunner.run_hook` end to end — argument
		mapping, interpreter selection, sidecar fetch ordering — without a
		network.  Set ``hook_results`` to script the values it returns.
		"""
		self.command_log.append(CommandRecord('run', list(cmd), cwd))
		payload = f"{RESULT_BEGIN}\n{json.dumps(self.hook_results)}\n{RESULT_END}\n"
		return ExecResult(0, payload, '')

	def submit_detached(self, cmd: list[str], cwd: str, job_name: str,
						timeout: float | None = None) -> JobHandle:
		self.command_log.append(CommandRecord('solver', list(cmd), cwd))
		self.clear_sentinels(cwd, job_name)
		self._poll_index = 0
		return JobHandle(job_name, cwd, pid=4242, method='recording', launch_rc=0)

	def poll(self, handle: JobHandle) -> int | None:
		if self._poll_index < len(self._poll_sequence):
			value = self._poll_sequence[self._poll_index]
			self._poll_index += 1
		else:
			value = self._poll_sequence[-1]
		return value

	def terminate(self, handle: JobHandle, abaqus_exe: str, grace_s: int) -> list[str]:
		self.command_log.append(
			CommandRecord('terminate', [abaqus_exe, 'terminate',
										f'job={handle.job_name}'], handle.work_dir))
		return ['recorded terminate']

	# ---- in-memory filesystem ----

	@staticmethod
	def _key(path: str) -> str:
		return path.replace('\\', '/').rstrip('/')

	def exists(self, path: str) -> bool:
		return self._key(path) in self.files

	def makedirs(self, path: str) -> None:
		return None

	def put(self, local_path: str, remote_path: str) -> int:
		try:
			with open(local_path, 'rb') as f:
				data = f.read()
		except OSError:
			data = b''
		self.files[self._key(remote_path)] = data
		return len(data)

	def put_text(self, text: str, remote_path: str) -> int:
		data = text.encode('utf-8')
		self.files[self._key(remote_path)] = data
		return len(data)

	def get(self, remote_path: str, local_path: str) -> bool:
		key = self._key(remote_path)
		if key not in self.files:
			return False
		parent = os.path.dirname(local_path)
		if parent:
			os.makedirs(parent, exist_ok=True)
		with open(local_path, 'wb') as f:
			f.write(self.files[key])
		self.fetched.append((remote_path, local_path))
		return True

	def glob_get(self, remote_dir: str, patterns: tuple[str, ...],
				local_dir: str) -> list[str]:
		prefix = self._key(remote_dir) + '/'
		out = []
		for key in sorted(self.files):
			if not key.startswith(prefix):
				continue
			name = key[len(prefix):]
			if '/' in name:
				continue
			if any(fnmatch.fnmatch(name, p) for p in patterns):
				if self.get(key, os.path.join(local_dir, name)):
					out.append(name)
		return out

	def remove(self, path: str) -> bool:
		return self.files.pop(self._key(path), None) is not None

	def read_text(self, path: str, max_bytes: int = 65536) -> str | None:
		data = self.files.get(self._key(path))
		if data is None:
			return None
		return data[:max_bytes].decode('utf-8', errors='replace')

	def close(self) -> None:
		return None

	# ---- inspection helpers ----

	def commands(self, stage: str | None = None) -> list[list[str]]:
		"""Recorded command lines, optionally filtered by *stage*."""
		return [r.cmd for r in self.command_log
				if stage is None or r.stage == stage]
