"""Job status enumeration and state machine — terminal-state protection.

Tracks every job through its lifecycle, from ``CREATED`` to ``COMPLETED``
or a terminal failure state.  Once a job enters a failure state no further
state transitions are allowed.
"""

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

	SIMULATING = "SIMULATING"
	SIMULATION_FAILED = "SIMULATION_FAILED"
	SIMULATION_SUCCESS = "SIMULATION_SUCCESS"

	EXTRACTING = "EXTRACTING"
	EXTRACTION_FAILED = "EXTRACTION_FAILED"
	EXTRACTION_SUCCESS = "EXTRACTION_SUCCESS"

	MONOLITHIC_SCRIPT_FAILED = "MONOLITHIC_SCRIPT_FAILED"
	JSON_DECODE_ERROR = "JSON_DECODE_ERROR"
	SCRIPT_ERROR = "SCRIPT_ERROR"
	UNKNOWN_ERROR = "UNKNOWN_ERROR"

	UNKNOWN = "UNKNOWN"


# Terminal failure states — once reached, no further state changes allowed (B4)
_TERMINAL_FAILURES = frozenset({
	JobStatus.PREPARATION_FAILED,
	JobStatus.SIMULATION_FAILED,
	JobStatus.EXTRACTION_FAILED,
	JobStatus.MONOLITHIC_SCRIPT_FAILED,
	JobStatus.JSON_DECODE_ERROR,
	JobStatus.SCRIPT_ERROR,
	JobStatus.UNKNOWN_ERROR,
})


class JobStatusManager:
	"""State machine for a single job with terminal-state protection.

	The manager tracks one job through its lifecycle.  Calling
	:meth:`record_preparation`, :meth:`record_simulation`, or
	:meth:`record_extraction` advances the state.  Once a terminal failure
	state is reached, all subsequent transitions are silently ignored — the
	first failure is the one that is kept.

	Attributes
	----------
	error_message : str or None
		Error message from the first terminal failure, or ``None``.
	"""

	def __init__(self):
		self._current_status: JobStatus = JobStatus.CREATED
		self._is_successful: bool = True
		self._error_message: str | None = None

	@property
	def error_message(self) -> str | None:
		"""Read-only access to the first-failure error message."""
		return self._error_message

	def _fail(self, status: JobStatus, msg: str):
		"""Transition to a terminal failure state (first-failure-wins).

		If the job is already in a terminal failure state this call is a
		no-op — only the original failure is preserved.

		Args
		----
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

	def record_preparation(self, success: bool, error: str = None):
		"""Record the outcome of the preparation phase.

		Args
		----
		success : bool
			``True`` if the INP was produced successfully.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if success:
			self._current_status = JobStatus.PREPARATION_SUCCESS
		else:
			self._fail(JobStatus.PREPARATION_FAILED, error or "Preparation step failed.")

	def record_simulation(self, success: bool, error: str = None):
		"""Record the outcome of the Abaqus solver run.

		Args
		----
		success : bool
			``True`` if the solver exited with code 0.
		error : str or None
			Error message on failure; a default is used if omitted.
		"""
		if self._current_status in _TERMINAL_FAILURES:
			return
		if success:
			self._current_status = JobStatus.SIMULATION_SUCCESS
		else:
			self._fail(JobStatus.SIMULATION_FAILED, error or "Simulation step failed.")

	def record_extraction(self, results: dict):
		"""Record extraction results; fails if any task returned ``None``.

		Args
		----
		results : dict
			``{result_name: value}`` mapping.  Any ``None`` value triggers
			``EXTRACTION_FAILED``.
		"""
		if any(v is None for v in results.values()):
			self._fail(JobStatus.EXTRACTION_FAILED,
					"One or more extraction tasks failed.")

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
