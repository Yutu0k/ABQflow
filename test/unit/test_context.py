"""Tests for ABQflow.core.context — JobContext path contract.

Run: pytest test/unit/test_context.py -v
"""

import os

from ABQflow import JobContext


def test_exec_log_path_distinct_from_native_log_path(tmp_path):
	"""ABQflow's own log path never collides with Abaqus's native job log."""
	ctx = JobContext(job_name='j1', output_dir=str(tmp_path), cpus=1)
	assert ctx.exec_log_path != ctx.log_path
	assert ctx.exec_log_path == os.path.join(str(tmp_path), 'j1_abqflow.log')
	assert ctx.log_path == os.path.join(str(tmp_path), 'j1.log')
