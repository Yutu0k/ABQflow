"""Data conversion utilities — array generation, result flattening, outcome serialisation.

These are the most commonly used helper functions extracted from the main
orchestrator so they can be imported lightweight without pulling in the
entire batch-processing machinery.
"""

from __future__ import annotations
import copy
import csv
import glob as glob_module
import os
import re
import warnings

import numpy as np

from ..core.spec import JobSpec, PreparationSpec


# ======================== IMP-06: sidecar CSV contract ========================

_SIDECAR_KEY = '__file__'


def is_sidecar(value) -> bool:
	"""Return ``True`` if *value* is a sidecar envelope (dict with ``__file__``).

	Parameters
	----------
	value : any
		Value to test.

	Returns
	-------
	bool
	"""
	return isinstance(value, dict) and _SIDECAR_KEY in value


def resolve_sidecar(value: dict, output_dir: str, load: bool = False):
	"""Resolve a sidecar envelope to an absolute path and optional data.

	Parameters
	----------
	value : dict
		Sidecar envelope: ``{'__file__': path, 'format': 'csv', ...}``.
	output_dir : str
		Directory that ``__file__`` is relative to.
	load : bool
		If ``True``, load the file and return a ``numpy.ndarray``.
		Default ``False`` (lazy — returns the absolute path).

	Returns
	-------
	tuple[str, dict] or tuple[numpy.ndarray, dict]
		``(absolute_path, metadata)`` when ``load=False``;
		``(ndarray, metadata)`` when ``load=True``.

	Raises
	------
	ValueError
		If the envelope is missing ``__file__`` or the file doesn't exist.
	"""
	if not is_sidecar(value):
		raise ValueError(f"Not a sidecar envelope (missing '{_SIDECAR_KEY}' key)")
	rel = value[_SIDECAR_KEY]
	abspath = os.path.normpath(os.path.join(output_dir, rel))
	if not os.path.isfile(abspath):
		raise ValueError(f"Sidecar file not found: {abspath}")
	if load:
		# ponytail: np.loadtxt covers CSV; add np.load for .npy when needed
		data = np.loadtxt(abspath, delimiter=',', skiprows=1)
		meta = {k: v for k, v in value.items() if k != _SIDECAR_KEY}
		return (data, meta)
	meta = {k: v for k, v in value.items() if k != _SIDECAR_KEY}
	return (abspath, meta)


# ======================== SC-01: sidecar field loading ========================


def load_field(outcome, result_name, numeric_only=True):
	"""Load a single named field from a ``JobOutcome``.

	Normalises inline values and sidecar CSV envelopes into a uniform
	``numpy.ndarray``.  This is the single-job entry point for consuming
	sidecar results; for batch consumption use :func:`iter_fields`.

	Parameters
	----------
	outcome : JobOutcome
		Outcome from :meth:`BatchAbaqusProcessor.run_batch`.
	result_name : str
		Key in ``outcome.results`` to load.
	numeric_only : bool
		If ``True`` (default), non-numeric CSV columns are dropped with a
		warning.  If ``False``, return an object array preserving string
		columns (for callers that need label columns).

	Returns
	-------
	numpy.ndarray or None
		``None`` when the result is missing, the extraction failed, the
		sidecar file is gone, or the path is unsafe.
	"""
	if outcome.results is None:
		return None

	value = outcome.results.get(result_name)
	if value is None:
		return None

	# Inline: scalar or list -> ndarray (dual-representation normalisation)
	if not is_sidecar(value):
		return np.asarray(value, dtype=float)

	# Sidecar envelope: resolve path
	output_dir = getattr(outcome, 'output_dir', None)
	if output_dir is None:
		warnings.warn(
			f"Cannot resolve sidecar '{result_name}' for job '{outcome.job_name}': "
			f"outcome has no output_dir"
		)
		return None

	rel = value[_SIDECAR_KEY]
	abspath = os.path.normpath(os.path.join(output_dir, rel))

	# Path safety check
	if not abspath.startswith(os.path.normpath(output_dir) + os.sep):
		warnings.warn(
			f"Sidecar path escape rejected: '{rel}' for job '{outcome.job_name}'"
		)
		return None

	# Existence check
	if not os.path.isfile(abspath) or os.path.getsize(abspath) == 0:
		warnings.warn(
			f"Sidecar file missing or empty: '{abspath}' for job '{outcome.job_name}'"
		)
		return None

	# Read CSV
	try:
		with open(abspath, 'r', newline='') as f:
			reader = csv.reader(f)
			header = next(reader)
			raw_rows = list(reader)
	except Exception as e:
		warnings.warn(
			f"Cannot read sidecar CSV '{abspath}' for '{result_name}': {e}"
		)
		return None

	# Header vs claimed columns
	claimed_columns = value.get('columns')
	if claimed_columns is not None and claimed_columns != header:
		warnings.warn(
			f"Sidecar columns mismatch for '{result_name}' in '{outcome.job_name}': "
			f"claimed {claimed_columns}, file has {header}"
		)

	# Shape vs reality
	claimed_shape = value.get('shape')
	actual_rows = len(raw_rows)
	actual_cols = len(header)
	if claimed_shape is not None:
		if claimed_shape[0] != actual_rows or claimed_shape[1] != actual_cols:
			warnings.warn(
				f"Sidecar shape mismatch for '{result_name}' in '{outcome.job_name}': "
				f"claimed {claimed_shape}, file has [{actual_rows}, {actual_cols}]"
			)

	if numeric_only:
		# Per-column numeric conversion — drop non-numeric columns
		numeric_cols = []
		for j, col_name in enumerate(header):
			try:
				col_data = [float(row[j]) for row in raw_rows]
				numeric_cols.append(col_data)
			except (ValueError, IndexError):
				warnings.warn(
					f"Non-numeric column '{col_name}' in '{result_name}' "
					f"for job '{outcome.job_name}' — dropped"
				)
		if not numeric_cols:
			return None
		return np.array(numeric_cols).T  # (R, C)
	else:
		return np.array(raw_rows, dtype=object)


def iter_fields(outcomes, result_name, on_missing='skip'):
	"""Yield ``(job_name, ndarray)`` pairs for a named field across a batch.

	Outcomes are sorted by :func:`_natural_key` on ``job_name`` so the
	iteration order is deterministic and matches the row order of
	:func:`degenerate_from_array` (row-order contract).

	Parameters
	----------
	outcomes : list[JobOutcome]
		Outcomes from :meth:`BatchAbaqusProcessor.run_batch`.
	result_name : str
		Key in ``outcome.results`` to load.
	on_missing : str
		How to handle jobs where :func:`load_field` returns ``None``:

		* ``'skip'`` (default) — omit the job; a single summary warning
		  lists all skipped job names at generator exit.
		* ``'none'`` — yield ``(job_name, None)`` so the caller can
		  align rows with :func:`degenerate_from_array`.
		* ``'raise'`` — raise :class:`ValueError` on the first missing field.

	Yields
	------
	tuple[str, numpy.ndarray or None]
		``(job_name, ndarray)`` pairs.  ``ndarray`` is ``None`` only when
		``on_missing='none'``.
	"""
	if on_missing not in ('skip', 'none', 'raise'):
		raise ValueError(
			f"on_missing must be 'skip', 'none', or 'raise', got '{on_missing}'"
		)

	sorted_outcomes = sorted(outcomes, key=lambda o: _natural_key(o.job_name))
	missing = []

	for oc in sorted_outcomes:
		arr = load_field(oc, result_name)
		if arr is None:
			if on_missing == 'raise':
				raise ValueError(
					f"Field '{result_name}' missing for job '{oc.job_name}'"
				)
			elif on_missing == 'skip':
				missing.append(oc.job_name)
			else:  # 'none'
				yield (oc.job_name, None)
		else:
			yield (oc.job_name, arr)

	if missing and on_missing == 'skip':
		warnings.warn(
			f"iter_fields('{result_name}'): {len(missing)} job(s) skipped "
			f"due to missing field: {missing}"
		)


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

		* ``'stem'`` (default) - use the INP filename without extension,
		sanitised via :func:`sanitize_job_name`.
		* ``'indexed'`` - ``{base_spec.job_name}_{i:04d}``.
		
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
		row = []
		for n in output_names:
			v = r.get(n, default_value)
			if is_sidecar(v):
				raise ValueError(
					f"'{n}' is a sidecar field. Load it with iter_fields() and "
					f"reduce it yourself, then hstack with this matrix (row order "
					f"is guaranteed to match)."
				)
			row.append(v)
		rows.append(row)

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
		if oc.diagnostics:
			d['diagnostics'] = oc.diagnostics
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
		if oc.diagnostics:
			d['diagnostics'] = oc.diagnostics
		out[oc.job_name] = d
	return out
