"""Data conversion utilities — array generation, result flattening, outcome serialisation.

These are the most commonly used helper functions extracted from the main
orchestrator so they can be imported lightweight without pulling in the
entire batch-processing machinery.
"""

from __future__ import annotations
import copy
import re
import warnings

import numpy as np

from ..core.spec import JobSpec


# ======================== Array generation / degeneration ========================

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
