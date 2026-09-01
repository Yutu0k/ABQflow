"""Pure builders for detached remote execution — no I/O, no network.

Kept separate from the SSH backend so every decision here is unit-testable
with plain strings, in the same spirit as
:meth:`AbaqusRunner.build_solver_command`.

Why the solver must be detached at all
--------------------------------------
Holding an SSH channel open for the duration of a solve makes the job as
fragile as the connection: a laptop sleeping, a Wi-Fi roam, a VPN reconnect
or sshd's ``ClientAliveInterval`` closes the channel, and Windows OpenSSH
terminates the session's process tree with it.  A six-hour job dies at hour
four.  So the solver is launched detached and progress is read from files on
the remote disk, which makes the poll loop stateless: it can lose the
connection, reconnect, and resume with nothing lost.

Measured on the target machines (2026-08-30): ``Win32_Process.Create``
survives channel close and reports a usable PID.  ``schtasks`` was rejected
outright, and ``start /b`` returned success and then never ran the batch file
at all — a *silent* failure, which is why the launcher is verified by looking
for output rather than by trusting a return code.
"""

from __future__ import annotations

import os
import re

# Grace-period bounds, matching AbaqusRunner's local ladder so the two stay
# comparable.
_GRACE_MIN = 30
_GRACE_MAX = 300

# Same directive shape ExistingInpStrategy matches; Abaqus accepts INPUT= with
# or without quotes and the keyword is case-insensitive.
_INCLUDE_RE = re.compile(
	r'(^\s*\*INCLUDE\s*,\s*INPUT\s*=\s*)(["\']?)([^"\'\r\n]+)\2',
	re.IGNORECASE | re.MULTILINE,
)


def find_includes(text: str) -> list[str]:
	"""Return the raw ``INPUT=`` paths of every ``*INCLUDE`` in *text*."""
	return [m.group(3).strip() for m in _INCLUDE_RE.finditer(text)]


def rewrite_includes(text: str, resolver) -> str:
	"""Rewrite every ``*INCLUDE, INPUT=`` path through *resolver*.

	``ExistingInpStrategy`` deliberately rewrites includes to **local
	absolute paths**, which are guaranteed not to exist on another machine —
	so every job with an include would fail remotely with a cryptic Abaqus
	preprocessing error.  This is the substitution step that fixes that; the
	caller decides *what* each path becomes.

	Kept free of I/O on purpose: the caller resolves and uploads first, then
	hands in a finished mapping, so this stays a pure function that a unit
	test can drive with plain strings.

	Parameters
	----------
	text : str
		INP contents.
	resolver : callable
		``(original_path) -> str or None``.  ``None`` leaves that directive
		untouched, which is what an unresolvable path should do — silently
		rewriting it to something that does not exist would be worse.

	Returns
	-------
	str
		The rewritten deck.
	"""
	def _sub(m: re.Match) -> str:
		raw = m.group(3).strip()
		replacement = resolver(raw)
		if replacement is None:
			return m.group(0)
		return m.group(1) + replacement

	return _INCLUDE_RE.sub(_sub, text)


def flatten_includes(text: str) -> tuple[str, list[str]]:
	"""Rewrite every ``*INCLUDE, INPUT=`` to a bare filename.

	Abaqus resolves a bare include filename against the job's working
	directory, so this makes a deck self-contained once its targets sit
	beside it.  Use it when the referenced files genuinely belong to one job;
	:meth:`AbaqusRunner.stage_inputs` instead points them at a shared
	directory, so a large mesh is uploaded once per machine rather than once
	per job.

	Returns
	-------
	tuple[str, list[str]]
		``(rewritten_text, original_paths)``.
	"""
	originals: list[str] = []

	def _resolve(raw: str) -> str:
		originals.append(raw)
		return os.path.basename(raw.replace('\\', '/'))

	return rewrite_includes(text, _resolve), originals


def build_launcher_bat(abaqus_exe: str, job_name: str, work_dir: str,
					cpus: int, user_subroutine: str | None = None) -> str:
	"""Render the ``.bat`` wrapper that runs the solver and writes the rc sentinel.

	``interactive`` is kept deliberately: it is what makes ``abaqus.bat``
	block, so ``%ERRORLEVEL%`` refers to the solver and not to the launcher.

	.. warning::
	   The parentheses around the final ``echo`` are load-bearing.
	   ``echo %ERRORLEVEL%> file`` expands to ``echo 0> file``, and cmd.exe
	   reads a digit immediately before ``>`` as a **file-descriptor number**
	   — so it redirects stdin and the sentinel ends up with no return code
	   in it.  Wrapping the echo in a command block keeps any digit away from
	   the redirection operator.  This was observed in practice, not theory.
	"""
	solver = f'call "{abaqus_exe}" job={job_name} input={job_name}.inp cpus={cpus}'
	if user_subroutine:
		solver += f' user="{user_subroutine}"'
	solver += f' interactive > {job_name}.abqflow.out 2>&1'

	return (
		'@echo off\r\n'
		f'cd /d "{work_dir}"\r\n'
		f'{solver}\r\n'
		f'(echo %ERRORLEVEL%)> {job_name}.abqflow.rc\r\n'
	)


def _ps_quote(value: str) -> str:
	"""Escape *value* for a PowerShell single-quoted string."""
	return value.replace("'", "''")


def build_detach_script(bat_path: str, work_dir: str) -> str:
	"""Render the PowerShell that launches *bat_path* detached.

	``Win32_Process.Create`` re-parents the new process to ``WmiPrvSE.exe``
	rather than the sshd session, so closing the SSH channel cannot reap it,
	and it returns the real PID that the terminate ladder needs.
	"""
	return (
		"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
		"-Arguments @{CommandLine='cmd /c \"" + _ps_quote(bat_path) + "\"'; "
		"CurrentDirectory='" + _ps_quote(work_dir) + "'}\n"
		"Write-Output $r.ReturnValue\n"
		"Write-Output $r.ProcessId\n"
	)


def parse_detach_output(stdout: str) -> tuple[int | None, int | None]:
	"""Parse ``(ReturnValue, ProcessId)`` from the launcher's output.

	``ReturnValue`` 0 means the process was created.
	"""
	nums = [ln.strip() for ln in (stdout or '').splitlines() if ln.strip().isdigit()]
	if not nums:
		return None, None
	rc = int(nums[0])
	pid = int(nums[1]) if len(nums) > 1 else None
	return rc, pid


def parse_rc_sentinel(text: str | None) -> int | None:
	"""Extract the solver return code from ``<job>.abqflow.rc`` contents."""
	if text is None:
		return None
	digits = ''.join(c for c in text if c.isdigit() or c == '-')
	try:
		return int(digits)
	except ValueError:
		return None


def poll_verdict(rc_present: bool, lck_present: bool,
				elapsed_s: float, timeout_s: float | None) -> str:
	"""Classify one poll tick as ``'running'`` / ``'finished'`` / ``'timeout'``.

	The rc sentinel is the only authoritative completion signal.  ``.lck``
	absence never means finished on its own: Abaqus does not create it for
	the first few seconds after launch (measured: still absent at t=2 s,
	present at t=5 s), so treating "no lck" as "done" reports success before
	the solver has started.
	"""
	if rc_present:
		return 'finished'
	if timeout_s is not None and elapsed_s >= timeout_s:
		return 'timeout'
	return 'running'


def grace_period(timeout_s: float | None) -> int:
	"""G = clamp(0.05 x T, 30, 300) seconds — same formula as the local ladder."""
	if timeout_s is None:
		return _GRACE_MAX
	return max(_GRACE_MIN, min(int(0.05 * timeout_s), _GRACE_MAX))


def quote_arg(arg: str) -> str:
	"""Quote *arg* for a Windows command line if it needs it."""
	if arg and not any(c in arg for c in ' \t"&|<>^()'):
		return arg
	return '"' + arg.replace('"', r'\"') + '"'


def build_cmd_line(argv: list[str]) -> str:
	"""Join *argv* into a single Windows command line."""
	return ' '.join(quote_arg(a) for a in argv)


def wrap_cmd(argv: list[str], cwd: str | None = None) -> str:
	"""Wrap *argv* as ``cmd /s /c "…"`` — shell-agnostic and quote-safe.

	``/s`` gives cmd's one well-defined quoting rule: strip exactly the first
	and last quote character and treat the rest literally.  Wrapping
	explicitly means the command works whether sshd's default shell is
	``cmd.exe`` or PowerShell, without touching the ``DefaultShell``
	registry key on the target.
	"""
	inner = build_cmd_line(argv)
	if cwd:
		inner = f'cd /d "{cwd}" && {inner}'
	return f'cmd /s /c "{inner}"'


def wrap_powershell(script: str) -> str:
	"""Wrap a PowerShell *script* as a quote-free ``-EncodedCommand`` call.

	Base64-of-UTF-16LE carries no quotes at all, so the command line survives
	any intermediate shell untouched — this removes an entire class of
	Windows quoting bugs rather than trying to escape around them.
	"""
	import base64
	encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
	return f'powershell -NoProfile -NonInteractive -EncodedCommand {encoded}'
