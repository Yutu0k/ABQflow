"""AbaqusRunner — encapsulates all subprocess calls. Strategy depends on this, not AbaqusCalculation."""

import json
import os
import uuid
import subprocess
import logging

from .context import JobContext
from .utils.helpers import check_abqpy_installed

# ---- Sentinel-based JSON extraction (fix B7/B13) ----
RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END = "===ABQ_RESULT_END==="


def extract_json(text: str) -> dict:
	"""Extract JSON from stdout. Sentinel-marker first, fallback to legacy brace scan from end."""
	if RESULT_BEGIN in text and RESULT_END in text:
		payload = text.split(RESULT_BEGIN, 1)[1].split(RESULT_END, 1)[0]
		return json.loads(payload)
	return _legacy_brace_scan(text)

def _legacy_brace_scan(text: str) -> dict:
	"""Scan from the *end* for the last complete JSON object (Abaqus banner is at the front)."""
	# Find last '{' and try to parse balanced braces from there
	last_brace = text.rfind('{')
	if last_brace == -1:
		raise ValueError("No '{' found in output.")
	candidate = text[last_brace:]
	try:
		return json.loads(candidate)
	except json.JSONDecodeError as e:
		try:
			return json.loads(candidate[:e.pos])
		except json.JSONDecodeError:
			raise ValueError(f"Failed to parse JSON from output: '{candidate[:100]}...'")


class AbaqusRunner:
	"""Encapsulates all subprocess calls. Fixes B5/B6/B10/B11.
	
	Methods
	-------
	run_solver() -> bool
		提交一个inp任务, 并求解. Return True on success, False on failure.
	run_hook(script_path, tasks, common_args, needs_cae_kernel) -> dict
		提交基于tasks的hook任务, 并返回结果字典. Failed tasks get None.


	
	"""

	def __init__(self, ctx: JobContext, logger: logging.Logger,
				timeout: float | None = None):
		self.ctx = ctx
		self.logger = logger
		self.timeout = timeout
		self._has_abqpy = check_abqpy_installed()

	# ---- Execution environment selection (fix B5/B6/B11) ----
	def _base_command(self, script: str, needs_cae_kernel: bool) -> list[str]:
		"""
		Pick the right interpreter.

		abqpy installed              → ['python', script]
		needs CAE kernel (mdb)       → [exe, 'cae', f'noGUI={script}', '--']
		only needs odbAccess         → [exe, 'python', script]

		The '--' separator is required so custom args don't get consumed by the Abaqus CLI.
		"""
		if self._has_abqpy:
			return ['python', script]
		if needs_cae_kernel:
			return [self.ctx.abaqus_exe, 'cae', f'noGUI={script}', '--']
		return [self.ctx.abaqus_exe, 'python', script]

	def run_solver(self) -> bool:
		"""提交一个inp任务, 并求解"""
		cmd = [self.ctx.abaqus_exe, f'job={self.ctx.job_name}', f'input={self.ctx.inp_path}', f'cpus={self.ctx.cpus}', 'interactive']
		return self._run(cmd, cwd=self.ctx.output_dir) is not None

	def run_hook(
		self,
		script_path: str,
		tasks: list[dict],
		common_args: dict[str, str],
		needs_cae_kernel: bool
	) -> dict:
		"""提交基于tasks的hook任务, 并返回结果字典. Failed tasks get None.

		Returns {result_name: value, ...}. Failed tasks get None.
		"""
		if not tasks:
			return {}

		tmp = os.path.join(self.ctx.output_dir, f"tasks_{uuid.uuid4().hex}.json")
		try:
			with open(tmp, 'w', encoding='utf-8') as f:
				json.dump(tasks, f)

			cmd = self._base_command(script_path, needs_cae_kernel)
			for k, v in common_args.items():
				cmd += [k, str(v)]
			cmd += ['--tasks_json', tmp]

			proc = self._run(cmd)
			if proc is None:
				return {t['result_name']: None for t in tasks}
			return extract_json(proc.stdout)
		finally:
			if os.path.exists(tmp):
				os.remove(tmp)

	def _run(self, cmd: list[str], cwd: str | None = None):
		"""
		使用subprocess.run()执行命令, 捕获异常并记录日志. cwd默认为ctx.output_dir.

		Parameters
		----------
		cmd : list[str]
			Command to run.
		cwd : str | None
			Working directory. Defaults to ctx.output_dir.

		Returns
		-------
		subprocess.CompletedProcess | None
			CompletedProcess on success, None on failure.
		"""
		try:
			return subprocess.run(cmd, cwd=cwd or self.ctx.output_dir, check=True, capture_output=True, text=True, timeout=self.timeout)
		except subprocess.TimeoutExpired:
			self.logger.error(f"Timeout ({self.timeout}s): {' '.join(cmd)}")
			return None
		except subprocess.CalledProcessError as e:
			self.logger.error(f"Command failed: {' '.join(cmd)}\n"
							f"STDERR:\n{e.stderr}\nSTDOUT:\n{e.stdout}")
			return None
