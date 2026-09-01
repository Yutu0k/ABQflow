# -*- coding: utf-8 -*-
"""hookkit — minimal extraction harness for Abaqus Python hooks.

Single file, stdlib only, Py2.7 / Py3 compatible.
Never imports ABQflow, odbAccess, abaqus, or numpy — it only manages
the protocol (argparse, JSON, sentinel output, sidecar CSV).

.. important::
   The Py2.7 half of that promise is real and load-bearing: Abaqus 2022 and
   earlier ship Python 2.7 as their ``abaqus python`` interpreter, and this
   file is imported by every hook running under it.  That rules out
   f-strings, ``open(..., encoding=...)`` and implicit old-style classes —
   any of which turn into an ``ImportError``/``SyntaxError`` that surfaces
   only on an older machine.  ``test/unit/test_hookkit_py27.py`` enforces
   this with an AST scan; run it before changing anything here.

Usage (ODB hook)::

	import os, sys
	sys.path.insert(0, os.getcwd())
	import hookkit

	def extract_one(odb_path, task):
		from odbAccess import openOdb
		with hookkit.opened(openOdb(path=odb_path)) as odb:
			...
			return hookkit.scalar(value)

	if __name__ == '__main__':
		hookkit.run(extract_one, source_arg='--odb_path')

Usage (INP / mdb hook)::

	hookkit.run(extract_one, source_arg='--inp_path')
"""


import argparse
import io
import json
import os
import sys

# ---------------------------------------------------------------------------
# Protocol constants — MUST match ABQflow.helpers.constant
# ---------------------------------------------------------------------------
RESULT_BEGIN = "===ABQ_RESULT_BEGIN==="
RESULT_END   = "===ABQ_RESULT_END==="
_SIDECAR_KEY = "__file__"

_SIDECAR_COUNT_THRESHOLD = 10000
_SIDECAR_SIZE_THRESHOLD  = 1000000   # 1 MB

_EMITTED = False
_JOB_NAME_KEY = "_hookkit_job_name"  # internal task key, avoids collision

# Py2's ``str`` is bytes, and an ``io.open(..., encoding=...)`` stream refuses
# them ("write() argument 1 must be unicode, not str").  Everything written to
# such a stream must go through _text().  On Py3 this is just ``str``.
try:                       # pragma: no cover - Python 2 only
	_text = unicode        # noqa: F821 - undefined on Py3 by design
except NameError:          # pragma: no cover - Python 3
	_text = str


# ======================== primitives ========================

def log(msg):
	"""Write *msg* + newline to stderr (Py2-safe)."""
	sys.__stderr__.write(str(msg) + "\n")
	sys.__stderr__.flush()


def scalar(v):
	"""Convert *v* to a plain ``float``.

	Handles Abaqus internal numeric types that don't serialise cleanly.
	"""
	return float(v)


def fail(name, reason):
	"""Explicitly produce ``None`` for *name* with a stderr log.

	Sugar so the user can write::

		return hookkit.fail(name, reason)

	instead of raising an exception — both produce ``None`` + log.
	"""
	log("Task '{0}' failed: {1}".format(name, reason))
	return None


def emit(results):
	"""Write *results* dict as sentinel-wrapped JSON to ``sys.__stdout__``.

	Idempotent: a second call in the same process raises ``RuntimeError``
	to prevent double-sentinel blocks (which would corrupt the parse).
	"""
	global _EMITTED
	if _EMITTED:
		raise RuntimeError("hookkit.emit() called twice — double-sentinel output prevented")
	_EMITTED = True
	payload = json.dumps(results, default=str, indent=2)
	sys.__stdout__.write(
		"{0}\n{1}\n{2}\n".format(RESULT_BEGIN, payload, RESULT_END)
	)
	sys.__stdout__.flush()


# ======================== context manager ========================

class _Opened(object):
	"""Context manager that calls ``obj.close()`` on exit.

	Py2.7 compatible (no ``@contextlib.contextmanager`` generator).
	Inherits ``object`` explicitly so it is a new-style class under Py2 —
	special-method lookup for ``with`` is only guaranteed for those.
	"""
	def __init__(self, obj):
		self.obj = obj

	def __enter__(self):
		return self.obj

	def __exit__(self, *args):
		try:
			self.obj.close()
		except Exception:
			pass
		return False


def opened(obj):
	"""Return a context manager that closes *obj* on exit.

	Usage::

		with hookkit.opened(openOdb(path=odb_path)) as odb:
			...
	"""
	return _Opened(obj)


# ======================== field / sidecar ========================

def _resolve_mode(task, explicit_mode):
	"""Resolution order:  task['output']  >  explicit_mode  >  'auto'."""
	mode = task.get('output')
	if mode is not None:
		return mode
	if explicit_mode is not None:
		return explicit_mode
	return 'auto'


def _should_sidecar(rows):
	"""Return ``True`` if *rows* exceeds the auto-sidecar threshold."""
	try:
		n = len(rows)
	except TypeError:
		return False
	if n > _SIDECAR_COUNT_THRESHOLD:
		return True
	try:
		size = len(json.dumps(rows, default=str))
	except (TypeError, ValueError):
		return False
	return size > _SIDECAR_SIZE_THRESHOLD


def _write_csv(rows, columns, csv_path):
	"""Write *rows* as CSV with header *columns*.

	Py2.7 compatible — uses ``io.open`` for ``encoding`` + ``newline``, and
	:data:`_text` for every value.  Under Py2 a plain ``str(...)`` here raises
	``TypeError: write() argument 1 must be unicode, not str``, which hookkit
	turns into a ``None`` result and a stderr line — a silently missing field
	rather than a visible crash, so it is worth getting right.
	"""
	parent = os.path.dirname(csv_path)
	if parent and not os.path.isdir(parent):
		os.makedirs(parent)

	with io.open(csv_path, 'w', encoding='utf-8', newline='') as f:
		f.write(u','.join(_text(c) for c in columns) + u'\n')
		f.writelines(u','.join(_text(v) for v in row) + u'\n' for row in rows)


def _make_envelope(task, rows, columns, n_rows, n_cols):
	"""Write CSV sidecar and return the envelope dict.

	Returns ``None`` on write failure (never an envelope to nowhere).
	"""
	result_name = task['result_name']
	job_name = task.get(_JOB_NAME_KEY, '')
	if job_name:
		file_name = '{0}_{1}.csv'.format(job_name, result_name)
	else:
		file_name = '{0}.csv'.format(result_name)
		log("hookkit: --job_name not provided, envelope file named "
			"'{0}'".format(file_name))

	csv_path = os.path.join(os.getcwd(), file_name)

	try:
		_write_csv(rows, columns, csv_path)
	except Exception as e:
		log("hookkit: failed to write sidecar CSV '{0}': {1}".format(file_name, e))
		return None

	return {
		_SIDECAR_KEY: file_name,
		'format':      'csv',
		'shape':       [n_rows, n_cols],
		'columns':     columns,
	}


def field(task, rows, columns, mode=None):
	"""Decide representation and return raw *rows* or a sidecar envelope.

	Parameters
	----------
	task : dict
		Task descriptor from the tasks JSON.  ``task['output']`` controls
		the mode (``'inline'`` / ``'file'`` / ``'auto'``) and wins over
		the *mode* argument.
	rows : list[list]
		Row-major data to be extracted.
	columns : list[str]
		Column names (CSV header when writing to file).
	mode : str or None
		``'inline'`` / ``'file'`` / ``'auto'``.  Only used when
		``task['output']`` is absent.  Default: ``'auto'``.

	Returns
	-------
	list or dict
		Raw *rows* for inline, or an envelope ``dict`` for file mode.
		Returns ``None`` if file write fails (promise-after-write).
	"""
	resolved = _resolve_mode(task, mode)
	n_rows = len(rows)
	n_cols = len(rows[0]) if n_rows > 0 else 0

	if resolved == 'inline':
		return rows

	if resolved == 'file':
		return _make_envelope(task, rows, columns, n_rows, n_cols)

	# 'auto': threshold-based decision
	if _should_sidecar(rows):
		return _make_envelope(task, rows, columns, n_rows, n_cols)
	return rows


# ======================== harness ========================

def run(extract_fn, source_arg='--odb_path'):
	"""Main harness — parse args, iterate tasks, emit results.

	The user only writes *extract_fn*.  Everything else (argparse, task
	iteration, try/except→None, sentinel output) is handled here.

	Parameters
	----------
	extract_fn : callable
		``(source_path, task_dict) -> value``.
		Raise an exception on failure — ``run()`` converts it to
		``None`` + stderr log for that task only (partial-failure
		semantics).
	source_arg : str
		CLI flag carrying the source path, e.g. ``'--odb_path'`` or
		``'--inp_path'``.
	"""
	# -- argparse -------------------------------------------------------
	parser = argparse.ArgumentParser()
	parser.add_argument(source_arg, type=str, required=True)
	parser.add_argument('--tasks_json', type=str, required=True)
	parser.add_argument('--job_name', type=str, default='')
	args, _unknown = parser.parse_known_args()

	# Convert --odb-path → args.odb_path
	key = source_arg
	while key.startswith('-'):
		key = key[1:]
	key = key.replace('-', '_')
	source_path = getattr(args, key)
	tasks_json_path = args.tasks_json
	job_name = args.job_name

	# -- read tasks -----------------------------------------------------
	try:
		# io.open, not the builtin: Py2's open() has no encoding keyword.
		with io.open(tasks_json_path, 'r', encoding='utf-8') as f:
			tasks = json.load(f)
	except Exception as e:
		log("Fatal: cannot read tasks_json '{0}': {1}".format(tasks_json_path, e))
		sys.exit(1)

	# -- process each task ----------------------------------------------
	results = {}
	for task in tasks:
		name = task['result_name']
		# Inject job_name so field() can name envelopes correctly
		task[_JOB_NAME_KEY] = job_name
		try:
			value = extract_fn(source_path, task)
			results[name] = value
		except Exception as e:
			results[name] = None
			log("Task '{0}' failed: {1}".format(name, e))

	# -- emit -----------------------------------------------------------
	emit(results)
