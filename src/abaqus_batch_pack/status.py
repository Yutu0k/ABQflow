# A status manager for all Abaqus jobs


from enum import Enum
from typing import Dict, Any

class JobStatus(Enum):
	CREATED = "CREATED"
	COMPLETED = "COMPLETED"

	PREPARING = "PREPARING"
	PREPARATION_FAILED = "PREPARING_FAILED"
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


class JobStatusManager:
	def __init__(self):
		self._current_status: JobStatus = JobStatus.CREATED
		self._is_successful: bool = True
		self._error_message: str = None

	def record_preparation(self, success: bool, error: str = None):
		if not success:
			self._is_successful = False
			self._current_status = JobStatus.PREPARATION_FAILED
			self.error_message = error or "Preparation step failed."
		else:
			self._current_status = JobStatus.PREPARATION_SUCCESS

	def record_simulation(self, success: bool, error: str = None):
		if not success:
			self._is_successful = False
			self._current_status = JobStatus.SIMULATION_FAILED
			self.error_message = error or "Simulation step failed."
		else:
			self._current_status = JobStatus.SIMULATION_SUCCESS

	def record_extraction(self, results: dict):
		# 只要有一个结果是None，就认为提取步骤有问题。
		if any(v is None for v in results.values()):
			if self._is_successful: 
				self._current_status = JobStatus.EXTRACTION_FAILED
				self.error_message = "One or more extraction tasks failed."
	
	def get_final_status(self) -> JobStatus:
		if self._is_successful:		# Preparation / Simulation没问题
			if self._current_status == JobStatus.EXTRACTION_FAILED:
				return JobStatus.EXTRACTION_FAILED
			return JobStatus.COMPLETED
		else:
			return self._current_status