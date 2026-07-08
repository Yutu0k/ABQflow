"""Data conversion utilities — array generation, result flattening, outcome serialisation.

These are the most commonly used helper functions extracted from the main
orchestrator so they can be imported lightweight without pulling in the
entire batch-processing machinery.
"""

from __future__ import annotations
import copy
import glob as glob_module
import os
import re
import warnings

import numpy as np

from ..core.spec import JobSpec, PreparationSpec


# ======================== Job name sanitisation ========================

# Abaqus job name rules: max 80 chars, start with letter, [A-Za-z0-9_-] only.
_JOB_NAME_ILLEGAL_RE = re.compile(r'[^A-Za-z0-9_-]')


def sanitize_job_name(name: str, max_len: int = 80) -> str:
	"""Clean *name* so it is a valid Abaqus job name.

	Replaces any character outside ``[A-Za-z0-9_-]`` with ``'_'``, collapses
	consecutive underscores, strips leading/trailing underscores, ensures the
	result starts with a letter, and truncates to *max_len*.

	Returns *name* unchanged if it is already valid.
	"""
	cleaned = _JOB_NAME_ILLEGAL_RE.sub('_', name)
	cleaned = re.sub(r'_+', '_', cleaned).strip('_')
	if not cleaned:
		cleaned = 'job'
	if not cleaned[0].isalpha():
		cleaned = 'j_' + cleaned
	return cleaned[:max_len]


# ======================== Array generation / degeneration ========================

def generate_from_inp_files(
	inp_files: list[str] | str,
	base_spec: JobSpec | dict,
	naming: str = 'stem',
	sort: bool = True,
) -> list[JobSpec]:
	"""Create N :class:`JobSpec` objects from a list (or glob) of existing INP files.

	This is the batch-spec generator for the UC-03 "pre-existing INP batch"
	use case.  Each INP file becomes a spec with ``kind='existing_inp'``.

	Parameters
	----------
	inp_files : list[str] or str
		List of INP paths, or a glob pattern (e.g. ``'./legacy/*.inp'``).
	base_spec : JobSpec or dict
		Template spec whose ``workflow``, extraction hooks, and other
		non-preparation fields are copied.  The ``preparation`` field is
		**overwritten** for each generated spec.
	naming : str
		Job-name generation rule:

		* ``'stem'`` (default) — use the INP filename without extension,
		sanitised via :func:`sanitize_job_name`.
		* ``'indexed'`` — ``{base_spec.job_name}_{i:04d}``.
	sort : bool
		If ``True`` (default), sort files by natural key order.

	Returns
	-------
	list[JobSpec]
		One spec per INP file, ready for :class:`~abaqus_batch_pack.abaqus_automation.BatchAbaqusProcessor`.

	Raises
	------
	ValueError
		If glob expands to zero files, or if sanitised stem names collide.
	"""
	# 1. Expand glob / normalise input
	if isinstance(inp_files, str):
		files = sorted(glob_module.glob(inp_files))
		if not files:
			raise ValueError(f"Glob pattern '{inp_files}' matched no files")
	else:
		files = list(inp_files)
		if not files:
			raise ValueError("inp_files list is empty")

	# 2. Sort (natural order)
	if sort:
		files.sort(key=lambda p: _natural_key(os.path.basename(p)))

	# 3. Normalise base_spec (check for dict, not JobSpec — autoreload-safe)
	if isinstance(base_spec, dict):
		base_spec = JobSpec.from_dict(base_spec)

	# 4. Generate specs
	specs = []
	seen_names: dict[str, str] = {}  # sanitised_name -> original_path (for conflict reporting)

	for i, path in enumerate(files):
		abspath = os.path.abspath(path)
		s = copy.deepcopy(base_spec)

		# Determine job_name
		if naming == 'stem':
			stem = os.path.splitext(os.path.basename(path))[0]
			raw = sanitize_job_name(stem)
			# Conflict detection
			if raw in seen_names:
				raise ValueError(
					f"Sanitised job_name collision: files '{seen_names[raw]}' and "
					f"'{path}' both map to '{raw}'. Rename the source files "
					f"or use naming='indexed'.")
			seen_names[raw] = path
			s.job_name = raw
		elif naming == 'indexed':
			s.job_name = f"{base_spec.job_name}_{i + 1:04d}"
		elif callable(naming):
			s.job_name = naming(path, i)
		else:
			raise ValueError(f"Unknown naming mode: '{naming}'")

		# Overwrite preparation (warn if base_spec already had one)
		if s.preparation is not None and s.preparation.kind not in ('existing_inp', ''):
			warnings.warn(
				f"Overwriting base_spec.preparation (kind='{s.preparation.kind}') "
				f"with kind='existing_inp' for file '{path}'")

		s.preparation = PreparationSpec(
			kind='existing_inp',
			source_path=abspath,
			params={},
			options=base_spec.preparation.options if base_spec.preparation else {}
		)
		s.meta = {'source_inp': abspath}		# 把源 INP 文件路径存储在 meta 中

		specs.append(s)

	return specs


def generate_from_array(samples_array, param_names, base_spec) -> list[JobSpec]:
	"""Create N :class:`JobSpec` objects from an (N, D) parameter array.

	Each row of *samples_array* becomes a new spec via :func:`copy.deepcopy`
	of *base_spec*, so every spec owns independent mutable state.

	Parameters
	----------
	samples_array : ndarray or Tensor
		Shape ``(N, D)`` parameter matrix.  Torch tensors are converted to
		NumPy internally.
	param_names : list[str]
		Length-D list of parameter names.
	base_spec : JobSpec or dict
		Template spec.  Dicts are upgraded via :meth:`JobSpec.from_dict`.

	Returns
	-------
	list[JobSpec]
		N specs with zero-padded names (e.g. ``job_0001``, ``job_0002``).

	Raises
	------
	ValueError
		If the array column count does not match ``len(param_names)``.
	"""
	if hasattr(samples_array, 'numpy'):
		samples_array = samples_array.numpy()

	n, d = samples_array.shape
	if d != len(param_names):
		raise ValueError(f"Dimension mismatch: array has {d} cols, param_names has {len(param_names)}")

	if not isinstance(base_spec, JobSpec):
		base_spec = JobSpec.from_dict(base_spec)

	specs = []
	for i in range(n):
		s = copy.deepcopy(base_spec)
		s.job_name = f"{base_spec.job_name}_{i+1:04d}"
		params = {k: float(v) for k, v in zip(param_names, samples_array[i, :].tolist())}
		if s.workflow == 'monolithic':
			s.monolithic_params = params
		else:
			if s.preparation is not None:
				s.preparation.params = params
		specs.append(s)
	return specs


def _natural_key(name: str):
	"""Split *name* into (text, int, text, ...) tuples for natural sort order.

	Ensures ``job_2`` sorts before ``job_10``.
	"""
	return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def degenerate_from_array(outcomes: list, output_names: list[str],
						default_value=np.nan, require_completed: bool = True) -> np.ndarray:
	"""Extract a 2D NumPy array of output values from a list of outcomes.

	Outcomes are sorted by natural key on ``job_name`` so rows appear in
	the order the jobs were generated.  Jobs that are not ``COMPLETED`` are
	filled with *default_value* and trigger a warning.

	Parameters
	----------
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.
	output_names : list[str]
		Keys to extract from each outcome's ``results`` dict.
	default_value : float
		Value to use for missing or non-completed results (default ``NaN``).
	require_completed : bool
		If ``True`` (default), warn when non-``COMPLETED`` jobs are
		encountered.

	Returns
	-------
	np.ndarray
		Shape ``(len(outcomes), len(output_names))`` float array.
	"""
	sorted_outcomes = sorted(outcomes, key=lambda o: _natural_key(o.job_name))

	rows = []
	bad = []
	for oc in sorted_outcomes:
		if require_completed and oc.status != "COMPLETED":
			bad.append(oc.job_name)
		r = oc.results or {}
		rows.append([r.get(n, default_value) for n in output_names])

	if bad:
		warnings.warn(f"{len(bad)} jobs not COMPLETED, rows contain default values: {bad}")

	return np.asarray(rows, dtype=float)


# ======================== Result conversion ========================

def outcomes_to_list(outcomes: list) -> list[dict]:
	"""Convert a list of :class:`JobOutcome` objects to a list of plain dicts.

	Convenience for callers that prefer the legacy list-of-dicts shape.

	Parameters
	----------
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.

	Returns
	-------
	list[dict]
		Each dict contains ``'job_name'``, ``'status'``, flattened results,
		and optionally ``'error'``.
	"""
	out = []
	for oc in outcomes:
		d = {**(oc.results or {}), 'status': oc.status, 'job_name': oc.job_name}
		if oc.error:
			d['error'] = oc.error
		out.append(d)
	return out


def outcomes_to_dict(outcomes: list) -> dict[str, dict]:
	"""Convert a list of :class:`JobOutcome` objects to a ``{job_name: {...}}`` dict.

	Parameters
	----------
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.

	Returns
	-------
	dict[str, dict]
		Each value dict contains ``'status'``, flattened results, and
		optionally ``'error'``.

	Raises
	------
	ValueError
		If two outcomes share the same ``job_name``.
	"""
	out = {}
	for oc in outcomes:
		if oc.job_name in out:
			raise ValueError(f"Duplicate job_name in dict output: {oc.job_name}")
		d = {**(oc.results or {}), 'status': oc.status}
		if oc.error:
			d['error'] = oc.error
		out[oc.job_name] = d
	return out
