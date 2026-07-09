"""JobContext — frozen data contract that strategies read but cannot mutate."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class JobContext:
	"""Immutable data contract holding all information a strategy can observe.

	Strategies depend on this object but cannot mutate it, preventing
	accidental cross-strategy side effects.  Every field is read-only
	after construction.

	Attributes
	----------
	job_name : str
		Unique identifier for this job (also used as directory name).
	output_dir : str
		Absolute path to the job's working directory.
	cpus : int
		Number of CPUs requested for the Abaqus solver run.
	abaqus_exe : str
		Path or command name for the Abaqus executable (default ``"abaqus"``).
	"""

	job_name: str
	output_dir: str
	cpus: int
	abaqus_exe: str = "abaqus"

	@property
	def inp_path(self) -> str:
		"""Absolute path to the input file (``<output_dir>/<job_name>.inp``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.inp")

	@property
	def odb_path(self) -> str:
		"""Absolute path to the output database (``<output_dir>/<job_name>.odb``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.odb")

	@property
	def log_path(self) -> str:
		"""Absolute path to the job log file (``<output_dir>/<job_name>.log``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.log")

	@property
	def sta_path(self) -> str:
		"""Absolute path to the status file (``<output_dir>/<job_name>.sta``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.sta")

	@property
	def msg_path(self) -> str:
		"""Absolute path to the message file (``<output_dir>/<job_name>.msg``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.msg")

	@property
	def dat_path(self) -> str:
		"""Absolute path to the data file (``<output_dir>/<job_name>.dat``)."""
		return os.path.join(self.output_dir, f"{self.job_name}.dat")
