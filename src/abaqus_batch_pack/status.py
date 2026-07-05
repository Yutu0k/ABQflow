"""Job status manager — fixed B1/B2/B3/B4."""

from enum import Enum


class JobStatus(Enum):
	CREATED = "CREATED"
	COMPLETED = "COMPLETED"

	PREPARING = "PREPARING"
	PREPARATION_FAILED = "PREPARATION_FAILED"      # B2: was "PREPARING_FAILED"
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
	def __init__(self):
		self._current_status: JobStatus = JobStatus.CREATED
		self._is_successful: bool = True
		self._error_message: str | None = None

	@property
	def error_message(self) -> str | None:           # B1: read-only accessor on correct field
		return self._error_message

	def _fail(self, status: JobStatus, msg: str):    # B4: first failure is terminal
		if self._current_status in _TERMINAL_FAILURES:
			return
		self._is_successful = False
		self._current_status = status
		self._error_message = msg

	def record_preparation(self, success: bool, error: str = None):
		if self._current_status in _TERMINAL_FAILURES:
			return                                 # B4: terminal state protection
		if success:
			self._current_status = JobStatus.PREPARATION_SUCCESS
		else:
			self._fail(JobStatus.PREPARATION_FAILED, error or "Preparation step failed.")

	def record_simulation(self, success: bool, error: str = None):
		if self._current_status in _TERMINAL_FAILURES:
			return                                 # B4: terminal state protection
		if success:
			self._current_status = JobStatus.SIMULATION_SUCCESS
		else:
			self._fail(JobStatus.SIMULATION_FAILED, error or "Simulation step failed.")

	def record_extraction(self, results: dict):
		if any(v is None for v in results.values()):
			self._fail(JobStatus.EXTRACTION_FAILED,           # B3: extraction failure IS failure
					"One or more extraction tasks failed.")

	def get_final_status(self) -> JobStatus:
		if self._current_status in _TERMINAL_FAILURES:
			return self._current_status
		return JobStatus.COMPLETED
