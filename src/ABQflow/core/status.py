"""Job status enumeration and state machine — terminal-state protection.

Tracks every job through its lifecycle, from ``CREATED`` to ``COMPLETED``
or a terminal failure state.  Once a job enters a failure state no further
state transitions are allowed.
"""

import time
from dataclasses import dataclass, field, asdict
from enum import Enum


class JobStatus(Enum):
	"""Lifecycle state for a single batch job.

	Key values
	----------
	CREATED
		Initial state — job has been constructed but not yet started.
	COMPLETED
		Terminal success — the full workflow finished without error.
	PREPARATION_FAILED
		Terminal failure — the preparation phase could not produce an INP.
	SIMULATION_FAILED
		Terminal failure — the Abaqus solver exited with an error.
	EXTRACTION_FAILED
		Terminal failure — one or more post-extraction tasks returned ``None``.
	MONOLITHIC_SCRIPT_FAILED
		Terminal failure — the monolithic script exited with a non-zero code.
	JSON_DECODE_ERROR
		Terminal failure — monolithic or hook script output could not be
		parsed as JSON.
	SCRIPT_ERROR
		Terminal failure — an unhandled exception occurred in a hook or
		monolithic script.
	SUBROUTINE_COMPILE_FAILED
		Terminal failure — ``abaqus make`` (user subroutine compilation)
		exited with an error.
	UNKNOWN_ERROR
		Terminal failure — an exception escaped the worker process.
	UNKNOWN
		Fallback value used when no explicit status is available.
	"""
	CREATED = "CREATED"
	COMPLETED = "COMPLETED"

	PREPARING = "PREPARING"
	PREPARATION_FAILED = "PREPARATION_FAILED"
	PREPARATION_SUCCESS = "PREPARATION_SUCCESS"

	PREFLIGHT_FAILED = "PREFLIGHT_FAILED"

	SIMULATING = "SIMULATING"
	SIMULATION_FAILED = "SIMULATION_FAILED"
	SIMULATION_SUCCESS = "SIMULATION_SUCCESS"

	EXTRACTING = "EXTRACTING"
	EXTRACTION_FAILED = "EXTRACTION_FAILED"
	EXTRACTION_SUCCESS = "EXTRACTION_SUCCESS"

	MONOLITHIC_SCRIPT_FAILED = "MONOLITHIC_SCRIPT_FAILED"
	JSON_DECODE_ERROR = "JSON_DECODE_ERROR"
	SCRIPT_ERROR = "SCRIPT_ERROR"
	SUBROUTINE_COMPILE_FAILED = "SUBROUTINE_COMPILE_FAILED"
	UNKNOWN_ERROR = "UNKNOWN_ERROR"

	UNKNOWN = "UNKNOWN"


# Terminal failure states — once reached, no further state changes allowed (B4)
_TERMINAL_FAILURES = frozenset({
	JobStatus.PREPARATION_FAILED,
	JobStatus.PREFLIGHT_FAILED,
	JobStatus.SIMULATION_FAILED,
	JobStatus.EXTRACTION_FAILED,
	JobStatus.MONOLITHIC_SCRIPT_FAILED,
	JobStatus.JSON_DECODE_ERROR,
	JobStatus.SCRIPT_ERROR,
	JobStatus.SUBROUTINE_COMPILE_FAILED,
	JobStatus.UNKNOWN_ERROR,
})


@dataclass
class PhaseRecord:
	"""One phase's start/end/outcome — the unit of a job's phase history.

	Attributes
	----------
	phase : str
		``'compile'`` | ``'preparation'`` | ``'preflight'`` |
		``'pre_extraction'`` | ``'simulation'`` | ``'post_extraction'``.
	status : str
		Phase outcome string (e.g. ``'RUNNING'`` while open, then a
		:class:`JobStatus` value or ``'PASSED'``/``'COMPILED'`` once closed).
	started_at : float or None
		``time.time()`` when the phase was opened.
	ended_at : float or None
		``time.time()`` when the phase was closed.
	duration_s : float or None
		``ended_at - started_at``, populated on close.
	error : str or None
		Error message if the phase failed.
	"""
	phase: str
	status: str = 'RUNNING'
	started_at: float | None = None
	ended_at: float | None = None
	duration_s: float | None = None
	error: str | None = None


class JobStatusManager:
	"""State machine for a single job with terminal-state protection.

	Calling :meth:`record_preparation`, :meth:`record_simulation`, or
	:meth:`record_extraction` advances the state.  Once a terminal failure
	state is reached, all subsequent transitions are silently ignored — the
	first failure is the one that is kept.

	The ``mark_*`` methods set the *live* in-progress status (``PREPARING``/
	``SIMULATING``/``EXTRACTING``) and open a :class:`PhaseRecord`; the
	paired ``record_*`` method closes it.  ``mark_*`` calls are optional —
	callers that only care about the final outcome (as before) can skip them
	and just call ``record_*`` directly.

	Attributes
	----------
	error_message : str or None
		Error message from the first terminal failure, or ``None``.
	"""

	def __init__(self):
		self._current_status: JobStatus = JobStatus.CREATED
		self._is_successful: bool = True
		self._error_message: str | None = None
		self._phase_history: list[PhaseRecord] = []
		self._open_phase_record: PhaseRecord | None = None

	@property
	def error_message(self) -> str | None:
		"""Read-only access to the first-failure error message."""
		return self._error_message

	@property
	def current_status(self) -> JobStatus:
		"""Read-only access to the *live* status (updated at phase start, not just at the end)."""
		return self._current_status

	@property
	def phase_history(self) -> list[dict]:
		"""Closed phases so far, as plain dicts (picklable across process boundaries)."""
		return [asdict(p) for p in self._phase_history]

	def _fail(self, status: JobStatus, msg: str):
		"""Transition to a terminal failure state (first-failure-wins).

		If the job is already in a terminal failure state this call is a
		no-op — only the original failure is preserved.

		Parameters
		----------
		status : JobStatus
			Must be a member of the internal ``_TERMINAL_FAILURES`` set.
		msg : str
			Human-readable error description.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		self._is_successful = False
		self._current_status = status
		self._error_message = msg

	# ---- phase open/close helpers ----

	def _open_phase(self, phase_name: str):
		if self._current_status in _TERMINAL_FAILURES:
			return
		self._open_phase_record = PhaseRecord(phase=phase_name, status='RUNNING', started_at=time.time())

	def _close_phase(self, status_value: str, error: str | None = None):
		if self._open_phase_record is None:
			return
		rec = self._open_phase_record
		rec.status = status_value
		rec.error = error
		rec.ended_at = time.time()
		rec.duration_s = rec.ended_at - rec.started_at
		self._phase_history.append(rec)
		self._open_phase_record = None

	# ---- phase-start markers (IMP: fix dead PREPARING/SIMULATING/EXTRACTING states) ----

	def mark_compiling(self):
		"""Mark the start of subroutine compilation."""
		self._open_phase('compile')

	def mark_preparing(self):
		"""Mark the start of the preparation phase."""
		if self._current_status not in _TERMINAL_FAILURES:
			self._current_status = JobStatus.PREPARING
		self._open_phase('preparation')

	def mark_preflight(self):
		"""Mark the start of the preflight check."""
		self._open_phase('preflight')

	def mark_simulating(self):
		"""Mark the start of the solver run."""
		if self._current_status not in _TERMINAL_FAILURES:
			self._current_status = JobStatus.SIMULATING
		self._open_phase('simulation')

	def mark_extracting(self, label: str = 'extraction'):
		"""Mark the start of an extraction phase.

		Parameters
		----------
		label : str
			``'pre_extraction'`` or ``'post_extraction'`` — distinguishes
			the two extraction phases in :attr:`phase_history`.
		"""
		if self._current_status not in _TERMINAL_FAILURES:
			self._current_status = JobStatus.EXTRACTING
		self._open_phase(label)

	# ---- phase-end recorders ----

	def record_compile(self, success: bool, error: str = None):
		"""Record the outcome of user-subroutine compilation.

		Parameters
		----------
		success : bool
			``True`` if ``abaqus make`` (or an equivalent compile step)
			succeeded.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if success:
			self._close_phase('COMPILED')
		else:
			msg = error or "Subroutine compilation failed."
			self._fail(JobStatus.SUBROUTINE_COMPILE_FAILED, msg)
			self._close_phase(JobStatus.SUBROUTINE_COMPILE_FAILED.value, msg)

	def record_preparation(self, success: bool, error: str = None):
		"""Record the outcome of the preparation phase.

		Parameters
		----------
		success : bool
			``True`` if the INP was produced successfully.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if success:
			self._current_status = JobStatus.PREPARATION_SUCCESS
			self._close_phase(JobStatus.PREPARATION_SUCCESS.value)
		else:
			msg = error or "Preparation step failed."
			self._fail(JobStatus.PREPARATION_FAILED, msg)
			self._close_phase(JobStatus.PREPARATION_FAILED.value, msg)

	def record_preflight(self, success: bool, error: str = None):
		"""Record the outcome of the preflight phase (IMP-04).

		Parameters
		----------
		success : bool
			``True`` if syntax/datacheck passed.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if not success:
			msg = error or "Preflight check failed."
			self._fail(JobStatus.PREFLIGHT_FAILED, msg)
			self._close_phase(JobStatus.PREFLIGHT_FAILED.value, msg)
		else:
			self._close_phase('PASSED')

	def record_simulation(self, success: bool, error: str = None):
		"""Record the outcome of the Abaqus solver run.

		Parameters
		----------
		success : bool
			``True`` if the solver exited with code 0.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if success:
			self._current_status = JobStatus.SIMULATION_SUCCESS
			self._close_phase(JobStatus.SIMULATION_SUCCESS.value)
		else:
			msg = error or "Simulation step failed."
			self._fail(JobStatus.SIMULATION_FAILED, msg)
			self._close_phase(JobStatus.SIMULATION_FAILED.value, msg)

	def record_extraction(self, results: dict):
		"""Record extraction results; fails if any task returned ``None``.

		On success this now sets :attr:`current_status` to
		``JobStatus.EXTRACTION_SUCCESS`` (previously a no-op — the final
		status reported by :meth:`get_final_status` is unaffected, since
		``EXTRACTION_SUCCESS`` is not a terminal state).

		Parameters
		----------
		results : dict
			``{result_name: value}`` mapping.  Any ``None`` value triggers
			``EXTRACTION_FAILED``.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if any(v is None for v in results.values()):
			msg = "One or more extraction tasks failed."
			self._fail(JobStatus.EXTRACTION_FAILED, msg)
			self._close_phase(JobStatus.EXTRACTION_FAILED.value, msg)
		else:
			self._current_status = JobStatus.EXTRACTION_SUCCESS
			self._close_phase(JobStatus.EXTRACTION_SUCCESS.value)

	def get_final_status(self) -> JobStatus:
		"""Return the current state or ``COMPLETED`` if no failure was recorded.

		Returns
		-------
		JobStatus
			The terminal failure state if one was reached, otherwise
			``JobStatus.COMPLETED``.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return self._current_status
		return JobStatus.COMPLETED
