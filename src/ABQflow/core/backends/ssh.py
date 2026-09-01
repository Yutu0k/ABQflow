"""SshBackend — run Abaqus on another Windows machine over SSH + SFTP.

``paramiko`` is imported lazily, inside :meth:`SshBackend.connect`, so a
local-only installation never needs it and ``import ABQflow`` keeps working
without it.  Install with ``pip install 'ABQflow[remote]'``.

Threading
---------
One backend instance belongs to one job and therefore to one worker thread.
paramiko's ``Transport`` tolerates concurrent channel opens, but a single
``SFTPClient`` does **not**, so nothing here is shared between threads.
Remote jobs are I/O-bound on this side — upload, launch, sleep, stat,
download — which is why the remote path uses threads rather than processes
(and incidentally sidesteps the fact that an ``SSHClient`` cannot be pickled
into a ``ProcessPoolExecutor``).
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
import time
from dataclasses import replace

from ..context import JobContext
from ..hosts import HostSpec
from ..remote_launch import (
	build_detach_script,
	grace_period,
	parse_detach_output,
	parse_rc_sentinel,
	wrap_cmd,
	wrap_powershell,
)
from .base import ExecResult, ExecutionBackend, JobHandle

_IMPORT_HINT = (
	"Remote execution requires paramiko. Install it with:\n"
	"    pip install 'ABQflow[remote]'\n"
	"or, under pixi:\n"
	"    pixi add paramiko"
)


def _sftp_path(path: str) -> str:
	"""Convert a Windows path to the forward-slash form the SFTP server wants."""
	return path.replace('\\', '/')


class SshBackend(ExecutionBackend):
	"""Execute on a remote Windows machine described by a :class:`HostSpec`.

	Parameters
	----------
	host : HostSpec
		Target machine.  Must have ``hostname`` and ``work_root`` set.
	logger : logging.Logger or None
		Optional logger for connection events.
	"""

	is_remote = True

	def __init__(self, host: HostSpec, logger=None):
		if not host.is_remote:
			raise ValueError(
				f"SshBackend needs a HostSpec with a hostname; got {host.name!r}"
			)
		self.host = host
		self.name = host.name
		self.logger = logger
		self._client = None
		self._sftp = None
		self._prefix: str | None = None   # '' or '/', learned on first use

	# ---- connection ----

	def connect(self):
		"""Open the SSH connection, importing paramiko on first use."""
		if self._client is not None:
			return self._client
		try:
			import paramiko
		except ImportError as e:  # pragma: no cover - depends on install extras
			raise ImportError(_IMPORT_HINT) from e

		client = paramiko.SSHClient()
		client.load_system_host_keys()
		# Remote hosts here are explicitly configured by the user, so an
		# unknown key is expected on first contact; it is recorded in the
		# user's known_hosts by load/save, and a *changed* key still raises
		# BadHostKeyException before any policy runs.
		client.set_missing_host_key_policy(paramiko.WarningPolicy())

		kwargs = {
			'hostname': self.host.hostname,   # bare IPv6 literal, no brackets
			'port': self.host.port,
			'username': self.host.username,
			'timeout': self.host.connect_timeout,
			'banner_timeout': self.host.connect_timeout,
			'auth_timeout': self.host.connect_timeout,
			'allow_agent': False,
			'look_for_keys': bool(self.host.key_filename),
		}
		if self.host.key_filename:
			kwargs['key_filename'] = self.host.key_filename
		else:
			kwargs['password'] = self.host.password

		client.connect(**kwargs)
		transport = client.get_transport()
		if transport is not None:
			transport.set_keepalive(30)
		self._client = client
		if self.logger:
			self.logger.info("Connected to %s (%s)", self.host.name, self.host.hostname)
		return client

	def reconnect(self, attempts: int = 5, delay: float = 3.0) -> bool:
		"""Re-establish a dropped connection.

		Always safe: every completion signal lives in a file on the remote
		disk, so a poll loop resumes with nothing lost.
		"""
		self.close()
		for i in range(attempts):
			try:
				self.connect()
				return True
			except Exception as e:
				if self.logger:
					self.logger.warning("Reconnect %d/%d failed: %s", i + 1, attempts, e)
				time.sleep(delay)
		return False

	def close(self) -> None:
		for attr in ('_sftp', '_client'):
			obj = getattr(self, attr, None)
			if obj is not None:
				try:
					obj.close()
				except Exception:
					pass
				setattr(self, attr, None)

	# ---- context mapping ----

	def map_context(self, ctx: JobContext) -> JobContext:
		"""Rewrite *ctx* to the remote job directory and this host's Abaqus.

		One :func:`dataclasses.replace` handles both the path problem and the
		"Abaqus lives elsewhere on that machine" problem, because every
		derived path on :class:`JobContext` is a property computed from
		``output_dir``.  The command builders therefore need no changes and
		``JobContext`` stays frozen and local-first.
		"""
		remote_dir = self.host.job_dir(ctx.job_name)

		# The subroutine is staged into the job directory by
		# AbaqusRunner.stage_inputs, so ``user=`` must point at the copy that
		# will exist there — not at the local path, which does not exist on
		# the far side.
		user_sub = ctx.user_subroutine
		if user_sub:
			user_sub = remote_dir + '\\' + os.path.basename(
				user_sub.replace('/', '\\'))

		return replace(
			ctx,
			output_dir=remote_dir,
			abaqus_exe=self.host.abaqus_exe,
			cpus=self.host.resolved_cpus(ctx.cpus),
			user_subroutine=user_sub,
		)

	# ---- SFTP path convention ----

	def _sftp_client(self):
		if self._sftp is None:
			self._sftp = self.connect().open_sftp()
		return self._sftp

	def _p(self, path: str) -> str:
		"""Remote path in whichever drive-letter convention this server uses.

		Win32-OpenSSH reports absolute paths as ``/D:/dir`` but most builds
		also accept ``D:/dir``.  Guessing wrong makes every SFTP call fail in
		a way that looks like a permissions problem, so the convention is
		detected once rather than assumed.
		"""
		p = _sftp_path(path)
		if self._prefix is None:
			self._detect_prefix(p)
		if self._prefix and not p.startswith('/'):
			return self._prefix + p
		return p

	def _detect_prefix(self, sample: str):
		self._prefix = ''  # provisional, so nested calls terminate
		if len(sample) < 2 or sample[1] != ':':
			return
		drive = sample[:2] + '/'
		sftp = self._sftp
		if sftp is None:
			return
		for candidate, prefix in ((drive, ''), ('/' + drive, '/')):
			try:
				sftp.stat(candidate)
				self._prefix = prefix
				return
			except IOError:
				continue

	# ---- synchronous execution ----

	def run(self, cmd: list[str], cwd: str, timeout: float | None = None) -> ExecResult:
		return self._exec(wrap_cmd(cmd, cwd), timeout)

	def run_powershell(self, script: str, timeout: float | None = None) -> ExecResult:
		return self._exec(wrap_powershell(script), timeout)

	@staticmethod
	def _decode(raw: bytes) -> str:
		"""Decode console output, tolerating a non-UTF-8 Windows code page.

		A Chinese-locale Windows console emits CP936; decoding it as UTF-8
		turns diagnostics into mojibake exactly when they need reading.
		Abaqus's own artifacts are unaffected — they arrive over SFTP as
		bytes — but cmd.exe and PowerShell errors come through here.
		"""
		for encoding in ('utf-8', 'cp936', 'cp1252'):
			try:
				return raw.decode(encoding)
			except UnicodeDecodeError:
				continue
		return raw.decode('utf-8', errors='replace')

	def _exec(self, line: str, timeout: float | None) -> ExecResult:
		try:
			client = self.connect()
			_stdin, stdout, stderr = client.exec_command(line, timeout=timeout)
			out = self._decode(stdout.read())
			err = self._decode(stderr.read())
			rc = stdout.channel.recv_exit_status()
			return ExecResult(rc, out, err)
		except Exception as e:
			return ExecResult(None, '', f'{type(e).__name__}: {e}')

	# ---- detached execution ----

	def submit_detached(self, cmd: list[str], cwd: str, job_name: str,
						timeout: float | None = None) -> JobHandle:
		"""Write a .bat wrapper, launch it via WMI, and return a pollable handle.

		The command is *not* run through the SSH channel directly: see
		:mod:`~ABQflow.core.remote_launch` for why the solver has to outlive
		the connection.
		"""
		from ..remote_launch import build_launcher_bat

		self.makedirs(cwd)
		self.clear_sentinels(cwd, job_name)

		# cmd is the solver command built against the *remote* context, so
		# argv[0] is this host's abaqus.bat and the paths are remote already.
		abaqus_exe = cmd[0]
		cpus = next((int(a.split('=', 1)[1]) for a in cmd if a.startswith('cpus=')), 1)
		user_sub = next((a.split('=', 1)[1] for a in cmd if a.startswith('user=')), None)

		bat_path = f'{cwd}\\run_{job_name}.bat'
		self.put_text(
			build_launcher_bat(abaqus_exe, job_name, cwd, cpus, user_sub),
			bat_path,
		)

		res = self.run_powershell(build_detach_script(bat_path, cwd), timeout=60)
		rc, pid = parse_detach_output(res.stdout)
		return JobHandle(job_name, cwd, pid, 'win32_process',
						launch_rc=rc if rc is not None else 1,
						launch_output=res.brief() if hasattr(res, 'brief')
						else f'rc={res.returncode} {res.stderr[:200]}')

	def poll(self, handle: JobHandle) -> int | None:
		"""Return the solver return code, or ``None`` while it is still running.

		Reconnects transparently: the answer is a file on the remote disk, so
		losing the connection costs nothing but a retry.
		"""
		try:
			text = self.read_text(handle.rc_path, 64)
		except Exception:
			if not self.reconnect():
				raise
			text = self.read_text(handle.rc_path, 64)
		return parse_rc_sentinel(text)

	def terminate(self, handle: JobHandle, abaqus_exe: str, grace_s: int) -> list[str]:
		"""Remote mirror of the local escalation ladder."""
		log: list[str] = []

		res = self.run([abaqus_exe, 'terminate', f'job={handle.job_name}'],
					handle.work_dir, timeout=60)
		log.append(f"level 1 abaqus terminate: rc={res.returncode}")

		grace = grace_s or grace_period(None)
		log.append(f"level 2 grace {grace}s")
		deadline = time.time() + grace
		while time.time() < deadline:
			if self.exists(handle.rc_path):
				log.append("level 2 job exited during grace period")
				return log
			time.sleep(min(5.0, max(1.0, grace / 10)))

		if handle.pid is not None:
			res = self.run(['taskkill', '/T', '/F', '/PID', str(handle.pid)],
						handle.work_dir, timeout=60)
			log.append(f"level 3 taskkill PID {handle.pid}: rc={res.returncode}")
		else:
			log.append("level 3 skipped — launcher reported no PID")

		removed = self.remove(handle.lck_path)
		log.append(f"level 4 remove .lck: {'removed' if removed else 'not present'}")
		return log

	# ---- filesystem ----

	def exists(self, path: str) -> bool:
		try:
			self._sftp_client().stat(self._p(path))
			return True
		except IOError:
			return False

	def makedirs(self, path: str) -> None:
		if not path:
			return
		sftp = self._sftp_client()
		remote = self._p(path).rstrip('/')
		parts = remote.split('/')
		current = parts[0]
		for part in parts[1:]:
			current = current + '/' + part
			if not part or part.endswith(':'):
				continue
			try:
				sftp.stat(current)
			except IOError:
				try:
					sftp.mkdir(current)
				except IOError:
					pass  # racing sibling, or a component we cannot create

	def put(self, local_path: str, remote_path: str) -> int:
		self.makedirs(os.path.dirname(remote_path))
		self._sftp_client().put(local_path, self._p(remote_path))
		return os.path.getsize(local_path)

	def put_text(self, text: str, remote_path: str) -> int:
		self.makedirs(os.path.dirname(remote_path))
		data = text.replace('\r\n', '\n').replace('\n', '\r\n').encode('utf-8')
		with self._sftp_client().open(self._p(remote_path), 'wb') as f:
			f.write(data)
		return len(data)

	def get(self, remote_path: str, local_path: str) -> bool:
		"""Download one file.

		SFTP is a **binary** transfer — no newline translation — so files
		arrive exactly as Abaqus wrote them, which is what lets the
		unmodified ``.sta``/``.msg`` parsers run against them.
		"""
		parent = os.path.dirname(local_path)
		if parent and not os.path.isdir(parent):
			os.makedirs(parent, exist_ok=True)
		try:
			self._sftp_client().get(self._p(remote_path), local_path)
			return True
		except IOError:
			return False

	def glob_get(self, remote_dir: str, patterns: tuple[str, ...],
				local_dir: str) -> list[str]:
		import stat as stat_mod
		try:
			entries = self._sftp_client().listdir_attr(self._p(remote_dir))
		except IOError:
			return []
		fetched = []
		for entry in entries:
			if stat_mod.S_ISDIR(entry.st_mode or 0):
				continue
			name = entry.filename
			if not any(fnmatch.fnmatch(name, p) for p in patterns):
				continue
			remote = posixpath.join(_sftp_path(remote_dir), name)
			if self.get(remote, os.path.join(local_dir, name)):
				fetched.append(name)
		return fetched

	def remove(self, path: str) -> bool:
		try:
			self._sftp_client().remove(self._p(path))
			return True
		except IOError:
			return False

	def read_text(self, path: str, max_bytes: int = 65536) -> str | None:
		try:
			with self._sftp_client().open(self._p(path), 'rb') as f:
				return f.read(max_bytes).decode('utf-8', errors='replace')
		except IOError:
			return None
