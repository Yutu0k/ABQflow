"""JobContext — frozen data contract. No internal dependencies, breaks circular import."""

from dataclasses import dataclass
import os


@dataclass(frozen=True)
class JobContext:
	"""All data a strategy can see. Frozen — strategies can't mutate the execution environment."""

	job_name: str
	output_dir: str
	cpus: int
	abaqus_exe: str = "abaqus"

	@property
	def inp_path(self) -> str:
		return os.path.join(self.output_dir, f"{self.job_name}.inp")

	@property
	def odb_path(self) -> str:
		return os.path.join(self.output_dir, f"{self.job_name}.odb")

	@property
	def log_path(self) -> str:
		return os.path.join(self.output_dir, f"{self.job_name}.log")
