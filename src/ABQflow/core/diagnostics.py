"""Solver diagnostics — .sta verdict, .msg/.dat error harvesting, truth table.

IMP-01 + IMP-02: Provides authoritative job success/failure determination by
cross-referencing the subprocess return code with the .sta file's completion
marker, and harvests actionable error lines from .msg/.dat files.
"""

from __future__ import annotations
import os
import re
from dataclasses import dataclass, asdict

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SolverDiagnostics:
	"""Diagnostic snapshot harvested after a solver run.

	Attributes
	----------
	sta_verdict : str
		Raw verdict from .sta parsing:
		``'COMPLETED'`` | ``'NOT_COMPLETED'`` | ``'ABORTED'`` | ``'INDETERMINATE'``.
	errors : list[str]
		Deduplicated, truncated error lines (at most *k_errors* entries).
	error_total : int
		Total ERROR lines found before dedup/truncation.
	warning_total : int
		Total WARNING lines found.
	increments : int
		Completed increment count from .sta (best-effort).
	source_files : dict[str, str]
		Map of kind (``'sta'``, ``'msg'``, ``'dat'``, ``'log'``) to absolute path
		for every file that was actually read.
	solver_type : str
		``'standard'`` | ``'explicit'`` | ``'unknown'``.
	"""
	sta_verdict: str = 'INDETERMINATE'
	errors: list[str] = None
	error_total: int = 0
	warning_total: int = 0
	increments: int = 0
	source_files: dict[str, str] = None
	solver_type: str = 'unknown'

	def __post_init__(self):
		if self.errors is None:
			self.errors = []
		if self.source_files is None:
			self.source_files = {}


@dataclass
class SolverResult:
	"""Returned by :meth:`AbaqusRunner.run_solver`.

	Wraps the raw diagnostics with the combined truth-table success
	determination so callers get a single authoritative answer.
	"""
	success: bool
	error: str | None = None
	diagnostics: SolverDiagnostics | None = None


# ---------------------------------------------------------------------------
# .sta parsing
# ---------------------------------------------------------------------------

# Standard solver completion markers (searched from end of file backwards)
_STA_STANDARD_COMPLETED = 'THE ANALYSIS HAS COMPLETED SUCCESSFULLY'
_STA_STANDARD_NOT_COMPLETED = 'THE ANALYSIS HAS NOT BEEN COMPLETED'

# Solver-type detection strings in .sta header
# Abaqus writes "Abaqus/Standard" or "Abaqus/Explicit" on the first line;
# older versions or certain locales may use spaced-out "S T A N D A R D".
_STA_STANDARD_MARKERS = ('STANDARD', 'S T A N D A R D')
_STA_EXPLICIT_MARKERS = ('EXPLICIT', 'E X P L I C I T')

# Increment-line pattern for Standard solver:
#   step  increment  attempt  time  dt  ...
# Each field is right-aligned; the line starts with whitespace then digits.
_STA_INCREMENT_RE = re.compile(r'^\s+\d+\s+\d+\s+\d+\s+', re.MULTILINE)

# Explicit solver increment lines have a similar leading numeric pattern
_STA_EXPLICIT_ROW_RE = re.compile(r'^\s+\d+\s+\d+\s+')


def parse_sta(path: str) -> tuple[str, int, str]:
	"""Parse an Abaqus .sta file for verdict, increment count, and solver type.

	Parameters
	----------
	path : str
		Absolute path to the ``.sta`` file.

	Returns
	-------
	tuple[str, int, str]
		``(verdict, increments, solver_type)`` where *verdict* is one of
		``'COMPLETED'``, ``'NOT_COMPLETED'``, ``'ABORTED'``, or
		``'INDETERMINATE'``.
	"""
	try:
		with open(path, 'r', errors='replace') as f:
			full = f.read()
	except (OSError, UnicodeDecodeError):
		return ('INDETERMINATE', 0, 'unknown')

	# -- solver type --------------------------------------------------------
	solver_type = 'unknown'
	head_upper = full[:4096].upper()
	if any(m in head_upper for m in _STA_STANDARD_MARKERS):
		solver_type = 'standard'
	elif any(m in head_upper for m in _STA_EXPLICIT_MARKERS):
		solver_type = 'explicit'

	# -- verdict (from tail) ------------------------------------------------
	tail = full[-8192:]  # last 8 KB covers the summary section
	verdict = 'INDETERMINATE'

	if solver_type in ('standard', 'unknown'):
		if _STA_STANDARD_COMPLETED in tail:
			verdict = 'COMPLETED'
		elif _STA_STANDARD_NOT_COMPLETED in tail:
			verdict = 'NOT_COMPLETED'
		elif 'ABORTED' in tail:
			verdict = 'ABORTED'

	if solver_type == 'explicit':
		tail_lines = tail.strip().split('\n')
		last_lines = '\n'.join(tail_lines[-5:]).upper()
		if 'ERROR' in last_lines:
			verdict = 'ABORTED'
		elif 'ABORTED' in last_lines:
			verdict = 'ABORTED'
		elif tail_lines:
			last_line = tail_lines[-1].strip()
			if last_line and (_STA_EXPLICIT_ROW_RE.match(tail_lines[-1])
							or _STA_STANDARD_COMPLETED in tail):
				verdict = 'COMPLETED'

	# -- increment count (from full file) -----------------------------------
	increments = 0
	if solver_type in ('standard', 'unknown'):
		increments = len(_STA_INCREMENT_RE.findall(full))
	elif solver_type == 'explicit':
		for line in full.split('\n'):
			if _STA_EXPLICIT_ROW_RE.match(line):
				increments += 1

	return (verdict, increments, solver_type)


# ---------------------------------------------------------------------------
# .msg / .dat error harvesting
# ---------------------------------------------------------------------------

# Patterns anchored at line start with optional whitespace and up to 3 '*'
_ERROR_RE = re.compile(r'^\s*\*{0,3}ERROR', re.IGNORECASE)
_WARNING_RE = re.compile(r'^\s*\*{0,3}WARNING', re.IGNORECASE)


def harvest_errors(
	msg_path: str | None,
	dat_path: str | None,
	k_errors: int = 5,
	k_chars: int = 500,
) -> tuple[list[str], int, int]:
	"""Stream-scan .msg and .dat files for ERROR / WARNING lines.

	Reads files line-by-line so multi-GB .msg files never blow memory.
	Consecutive identical errors are folded into one entry with a repeat
	count.  Results are truncated to at most *k_errors* entries, each
	clipped to *k_chars* characters.

	Parameters
	----------
	msg_path : str or None
		Path to the ``.msg`` file (may be ``None`` or missing).
	dat_path : str or None
		Path to the ``.dat`` file (may be ``None`` or missing).
	k_errors : int
		Maximum number of error lines to retain (default 5).
	k_chars : int
		Maximum characters per retained error line (default 500).

	Returns
	-------
	tuple[list[str], int, int]
		``(errors, error_total, warning_total)``.
	"""
	errors: list[str] = []
	error_total = 0
	warning_total = 0

	prev_error: str | None = None
	repeat_count = 0

	def _flush():
		nonlocal prev_error, repeat_count
		if prev_error is not None:
			entry = prev_error[:k_chars]
			if repeat_count > 1:
				entry = f"{entry}  [repeated {repeat_count} times]"
			errors.append(entry)
			prev_error = None
			repeat_count = 0

	def _process_line(line: str):
		nonlocal error_total, warning_total, prev_error, repeat_count
		if _ERROR_RE.match(line):
			error_total += 1
			normalized = line.strip()
			if normalized == prev_error:
				repeat_count += 1
			else:
				_flush()
				prev_error = normalized
				repeat_count = 1
				if len(errors) >= k_errors:
					# Budget exhausted — keep counting totals but stop storing
					prev_error = None
					repeat_count = 0
		elif _WARNING_RE.match(line):
			warning_total += 1

	for path in (msg_path, dat_path):
		if not path or not os.path.isfile(path):
			continue
		try:
			with open(path, 'r', errors='replace') as f:
				for line in f:
					_process_line(line)
		except OSError:
			continue

	_flush()

	# Truncate to budget (re-guard in case _flush pushed us over)
	return (errors[:k_errors], error_total, warning_total)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def diagnose(job_name: str, work_dir: str) -> SolverDiagnostics:
	"""Run full diagnostics on a completed (or failed) Abaqus job.

	Reads files in the following order of importance:

	1. ``.dat`` — pre-processing errors (INP syntax, mesh, materials).
	If the job died before the analysis phase the .sta may not exist;
	.dat is the only clue.
	2. ``.msg`` — Standard solver errors & warnings (convergence, increments).
	3. ``.sta`` — authoritative completion marker + increment progress.
	Explicit solver errors also appear in the .sta tail.
	4. ``.log`` — fallback: the one-line ``COMPLETED`` / ``exited with errors``
	conclusion.

	Parameters
	----------
	job_name : str
		Abaqus job name (used to derive file names).
	work_dir : str
		Job output directory containing the result files.

	Returns
	-------
	SolverDiagnostics
		Populated diagnostic snapshot.
	"""
	sta_path = os.path.join(work_dir, f"{job_name}.sta")
	msg_path = os.path.join(work_dir, f"{job_name}.msg")
	dat_path = os.path.join(work_dir, f"{job_name}.dat")
	log_path = os.path.join(work_dir, f"{job_name}.log")

	source_files: dict[str, str] = {}

	# 1. .sta — verdict + increments + solver type
	verdict = 'INDETERMINATE'
	increments = 0
	solver_type = 'unknown'
	if os.path.isfile(sta_path):
		source_files['sta'] = sta_path
		verdict, increments, solver_type = parse_sta(sta_path)

	# 2. .msg + .dat — error harvesting
	if os.path.isfile(msg_path):
		source_files['msg'] = msg_path
	if os.path.isfile(dat_path):
		source_files['dat'] = dat_path

	errors, error_total, warning_total = harvest_errors(
		msg_path if os.path.isfile(msg_path) else None,
		dat_path if os.path.isfile(dat_path) else None,
	)

	# 3. .log — fallback ABORTED detection
	if verdict == 'INDETERMINATE' and os.path.isfile(log_path):
		source_files['log'] = log_path
		try:
			with open(log_path, 'r', errors='replace') as f:
				f.seek(0, os.SEEK_END)
				size = f.tell()
				f.seek(max(0, size - 4096))
				if 'ABORTED' in f.read():
					verdict = 'ABORTED'
		except OSError:
			pass

	return SolverDiagnostics(
		sta_verdict=verdict,
		errors=errors,
		error_total=error_total,
		warning_total=warning_total,
		increments=increments,
		source_files=source_files,
		solver_type=solver_type,
	)


# ---------------------------------------------------------------------------
# Truth table (IMP-02 core)
# ---------------------------------------------------------------------------

def apply_truth_table(returncode: int, sta_verdict: str) -> tuple[bool, str | None]:
	"""Combine subprocess return code and .sta verdict into a single judgment.

	The principle: **.sta's ``COMPLETED`` marker is the only success
	certificate; the return code is merely corroborating evidence.**

	==========  ===================  ==============
	returncode  sta_verdict           result
	==========  ===================  ==============
	0           COMPLETED             **success**
	0           NOT_COMPLETED/ABORTED failure
	0           INDETERMINATE         failure (suspicious — rc=0 but no marker)
	≠0          COMPLETED             **success** (warning: cleanup error)
	≠0          any other             failure
	==========  ===================  ==============

	Parameters
	----------
	returncode : int
		Subprocess exit code (0 = clean exit).
	sta_verdict : str
		Verdict from :func:`parse_sta`.

	Returns
	-------
	tuple[bool, str | None]
		``(is_success, warning_message)``.  *warning_message* is only
		populated for the ``rc≠0 + COMPLETED`` edge case.
	"""
	if returncode == 0 and sta_verdict == 'COMPLETED':
		return (True, None)
	if returncode == 0 and sta_verdict in ('NOT_COMPLETED', 'ABORTED'):
		return (False, None)
	if returncode == 0:  # INDETERMINATE
		return (False, None)
	if returncode != 0 and sta_verdict == 'COMPLETED':
		return (True, f"rc={returncode} but .sta reports COMPLETED — probable cleanup error, results valid")
	# returncode != 0, any other verdict
	return (False, None)
