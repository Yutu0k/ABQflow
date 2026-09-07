# -*- coding: utf-8 -*-
"""Stand-alone self-check for a hookkit extraction script.

Reproduces exactly what :meth:`~ABQflow.AbaqusRunner.run_hook` does — stage
``hookkit.py`` into the working directory, write a tasks JSON beside it, build
the interpreter command line, run it there — and then grades the output against
the contract the framework will later hold it to.  Use it after writing a new
hook and before wiring it into a :class:`~ABQflow.HookSpec`: a hook that passes
here will run inside ABQflow, and a hook that fails here would have failed
there with far less to go on.

The command line and the checks are not reimplemented — they come from
:meth:`~ABQflow.AbaqusRunner.build_script_command`,
:func:`~ABQflow.extract_json` and ``AbaqusRunner._validate_envelope``, the very
functions the framework uses, so this tool cannot drift away from it.

Installed as the ``abqflow-check-hook`` command; equivalently runnable as
``python -m ABQflow.check_hook``.

Usage
-----
ODB hook (runs under ``abaqus python``)::

	abqflow-check-hook hooks/get_my_results.py \\
		--odb D:/proj/output/myjob/myjob.odb \\
		--task max_stress_mises --task mises_field:file

DAT hook (plain text — runs under this Python, no Abaqus, no license)::

	abqflow-check-hook hooks/get_my_dat.py \\
		--dat D:/proj/output/myjob/myjob.dat \\
		--task max_stress_mises --task mises_field:file

INP pre-extraction hook (needs the CAE kernel)::

	abqflow-check-hook hooks/get_total_mass.py \\
		--inp D:/proj/output/myjob/myjob.inp --task total_mass

Exit code is 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import logging
import os
import shutil
import subprocess
import tempfile

from .core.runner import (
	_SUPPORT_SRC_DIR,
	AbaqusRunner,
	_check_abqpy_installed,
	extract_json,
)
from .helpers.constant import RESULT_BEGIN, RESULT_END

# ---------------------------------------------------------------------------
# Source kinds — one row per HookSpec.source, mirroring the strategies
# ---------------------------------------------------------------------------

class _Kind:
	"""How one artifact kind is handed to a hook.

	Mirrors :class:`~ABQflow.core.strategies.OdbExtractionStrategy` and friends:
	the CLI flag they pass, whether the CAE kernel is needed, which interpreter
	runs the script, and what gets staged next to ``hookkit.py``.
	"""

	def __init__(self, name, source_arg, needs_cae_kernel, interpreter, extra_modules):
		self.name = name
		self.source_arg = source_arg
		self.needs_cae_kernel = needs_cae_kernel
		self.interpreter = interpreter
		self.extra_modules = extra_modules

	@property
	def runs_under_abaqus(self) -> bool:
		return self.interpreter == 'abaqus'


KINDS = {
	# post_extraction, source='odb' — odbAccess, not mdb
	'odb': _Kind('odb', '--odb_path', False, 'abaqus', ()),
	# post_extraction, source='dat' — plain text, host Python, datkit staged
	'dat': _Kind('dat', '--dat_path', False, 'host', ('datkit.py',)),
	# pre_extraction — reads the INP through the CAE kernel
	'inp': _Kind('inp', '--inp_path', True, 'abaqus', ()),
}


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class Report:
	"""Collects graded checks and prints them as they happen."""

	def __init__(self):
		self.failures = 0
		self.warnings = 0

	def ok(self, label, detail=''):
		self._line('OK  ', label, detail)

	def warn(self, label, detail=''):
		self.warnings += 1
		self._line('WARN', label, detail)

	def fail(self, label, detail=''):
		self.failures += 1
		self._line('FAIL', label, detail)

	def skip(self, label, detail=''):
		self._line('SKIP', label, detail)

	@staticmethod
	def _line(tag, label, detail):
		print('[{0}] {1}'.format(tag, label))
		if detail:
			for line in str(detail).rstrip().splitlines():
				print('       {0}'.format(line))


# ---------------------------------------------------------------------------
# Check 1 — the script parses, and survives Python 2.7 if it must
# ---------------------------------------------------------------------------

_PY3_ONLY_MODULES = {
	'pathlib', 'typing', 'dataclasses', 'enum', 'statistics', 'secrets',
	'asyncio', 'concurrent', 'contextvars', 'zoneinfo', 'tomllib', 'graphlib',
}

_PY3_ONLY_NODES = {
	'NamedExpr': 'walrus operator :=',
	'JoinedStr': 'f-string',
	'AsyncFunctionDef': 'async def',
	'Await': 'await',
	'AsyncFor': 'async for',
	'AsyncWith': 'async with',
	'MatchValue': 'match statement',
}


def scan_py27(tree) -> list[str]:
	"""Return Python-2.7 incompatibilities in *tree*.

	The same rule set ``test/unit/test_hookkit_py27.py`` enforces on the staged
	modules, applied to a user hook: Abaqus 2022 and earlier ship Python 2.7 as
	``abaqus python``, and every one of these is an import-time ``SyntaxError``
	there while passing silently on a newer machine.
	"""
	offenders = []

	for node in ast.walk(tree):
		kind = _PY3_ONLY_NODES.get(type(node).__name__)
		if kind:
			offenders.append('{0} at line {1}'.format(kind, getattr(node, 'lineno', '?')))

		# builtin open() has no encoding/newline/errors keyword under Py2
		if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
				and node.func.id == 'open':
			if any(kw.arg in ('encoding', 'newline', 'errors') for kw in node.keywords):
				offenders.append(
					'open(..., encoding=/newline=/errors=) at line {0} — use io.open()'
					.format(node.lineno))

		# annotations are Python 3 syntax
		if isinstance(node, ast.AnnAssign):
			offenders.append('variable annotation at line {0}'.format(node.lineno))
		elif isinstance(node, ast.FunctionDef):
			if node.returns is not None:
				offenders.append('return annotation on {0}()'.format(node.name))
			for arg in list(node.args.args) + list(node.args.kwonlyargs):
				if arg.annotation is not None:
					offenders.append(
						"annotated arg '{0}' in {1}()".format(arg.arg, node.name))

		# class Foo: is old-style under Py2, and `with` needs new-style
		if isinstance(node, ast.ClassDef) and not node.bases:
			offenders.append(
				"class {0} has no explicit base — write 'class {0}(object):'"
				.format(node.name))

	roots = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			roots.update(alias.name.split('.')[0] for alias in node.names)
		elif isinstance(node, ast.ImportFrom) and node.module:
			roots.add(node.module.split('.')[0])

	for missing in sorted(roots & _PY3_ONLY_MODULES):
		offenders.append("imports '{0}', absent from Python 2.7".format(missing))
	if 'ABQflow' in roots:
		offenders.append(
			"imports ABQflow — not installed in the Abaqus interpreter; "
			"use the staged hookkit/datkit instead")

	return offenders


def check_script(report, hook_path, kind) -> bool:
	"""Grade the hook source itself: exists, parses, calls hookkit.run()."""
	if not os.path.isfile(hook_path):
		report.fail('hook script exists', hook_path)
		return False

	with open(hook_path, 'r', encoding='utf-8') as f:
		source = f.read()
	try:
		tree = ast.parse(source, filename=hook_path)
	except SyntaxError as exc:
		report.fail('hook script parses', '{0} at line {1}'.format(exc.msg, exc.lineno))
		return False
	report.ok('hook script parses', hook_path)

	if 'hookkit.run' not in source:
		report.warn(
			'calls hookkit.run()',
			"no 'hookkit.run(' found — without it the script emits no sentinel "
			"block and ABQflow cannot read a single result")
	else:
		report.ok('calls hookkit.run()')

	if kind.runs_under_abaqus:
		offenders = scan_py27(tree)
		if offenders:
			report.warn(
				'Python 2.7 compatible',
				'Abaqus 2022 and earlier run this under Python 2.7:\n  ' +
				'\n  '.join(offenders))
		else:
			report.ok('Python 2.7 compatible')
	else:
		report.skip('Python 2.7 compatible',
					'{0} hooks run under this Python'.format(kind.name))
	return True


# ---------------------------------------------------------------------------
# Check 2 — tasks
# ---------------------------------------------------------------------------

def build_tasks(report, task_specs, tasks_json):
	"""Return the task list, from ``--tasks-json`` or repeated ``--task``."""
	if tasks_json:
		if not os.path.isfile(tasks_json):
			report.fail('tasks JSON exists', tasks_json)
			return None
		try:
			with open(tasks_json, 'r', encoding='utf-8') as f:
				tasks = json.load(f)
		except ValueError as exc:
			report.fail('tasks JSON parses', str(exc))
			return None
	else:
		tasks = []
		for spec in task_specs:
			name, _, mode = spec.partition(':')
			task = {'result_name': name}
			if mode:
				task['output'] = mode
			tasks.append(task)

	if not isinstance(tasks, list) or not tasks:
		report.fail('tasks is a non-empty list', repr(tasks))
		return None
	for task in tasks:
		if not isinstance(task, dict) or 'result_name' not in task:
			report.fail("every task has a 'result_name'", repr(task))
			return None
	for task in tasks:
		mode = task.get('output')
		if mode is not None and mode not in ('inline', 'file', 'auto'):
			report.warn("task['output'] is inline/file/auto",
						"'{0}' in task '{1}' — hookkit treats anything else as "
						"auto".format(mode, task['result_name']))

	report.ok('tasks are well-formed',
				', '.join(t['result_name'] for t in tasks))
	return tasks


# ---------------------------------------------------------------------------
# Check 3 — staging + execution, exactly as run_hook does it
# ---------------------------------------------------------------------------

def stage(report, workdir, kind) -> bool:
	"""Copy ``hookkit.py`` (and any extra module) into *workdir*."""
	staged = []
	for module in ('hookkit.py',) + kind.extra_modules:
		src = os.path.join(_SUPPORT_SRC_DIR, module)
		if not os.path.isfile(src):
			report.fail('stage {0}'.format(module), 'not found at ' + src)
			return False
		shutil.copy2(src, os.path.join(workdir, module))
		staged.append(module)
	report.ok('staged into working dir', '{0} -> {1}'.format(', '.join(staged), workdir))
	return True


def run_hook(report, hook_path, artifact, kind, tasks, workdir, job_name,
				abaqus_exe, has_abqpy, timeout):
	"""Build the command line the way ABQflow does and execute it in *workdir*."""
	tasks_path = os.path.join(workdir, 'tasks_check_hook.json')
	with open(tasks_path, 'w', encoding='utf-8') as f:
		json.dump(tasks, f)

	cmd = AbaqusRunner.build_script_command(
		os.path.abspath(hook_path), kind.needs_cae_kernel, abaqus_exe,
		has_abqpy, interpreter=kind.interpreter)
	cmd += [kind.source_arg, os.path.abspath(artifact)]
	cmd += ['--job_name', job_name]
	cmd += ['--tasks_json', tasks_path]

	print('\n  $ cd {0}'.format(workdir))
	print('  $ {0}\n'.format(subprocess.list2cmdline(cmd)))

	try:
		proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
								errors='replace', timeout=timeout)
	except FileNotFoundError:
		report.fail('interpreter is launchable',
					"cannot run '{0}' — is Abaqus on PATH? Pass --abaqus-exe with "
					"the full path to abaqus.bat".format(cmd[0]))
		return None
	except subprocess.TimeoutExpired:
		report.fail('hook finished', 'timed out after {0}s'.format(timeout))
		return None

	if proc.returncode == 0:
		report.ok('exit code 0')
	else:
		report.fail('exit code 0',
					'got {0} — a hook whose tasks all failed still exits 0, so a '
					'non-zero code means the script itself crashed'.format(proc.returncode))
	return proc


# ---------------------------------------------------------------------------
# Check 4 — the output protocol
# ---------------------------------------------------------------------------

def check_output(report, proc, tasks):
	"""Grade stdout: sentinel pairing, JSON payload, per-task values."""
	begins = proc.stdout.count(RESULT_BEGIN)
	ends = proc.stdout.count(RESULT_END)
	if begins == 1 and ends == 1:
		report.ok('exactly one sentinel block')
	elif begins == 0:
		report.fail(
			'exactly one sentinel block',
			'no {0} in stdout — the script never reached hookkit.run()/emit(). '
			"Check the 'if __name__' block.\nstdout tail: {1}"
			.format(RESULT_BEGIN, proc.stdout[-400:].strip() or '(empty)'))
		return None
	else:
		report.fail('exactly one sentinel block',
					'{0} begin / {1} end markers — emit() ran more than once and '
					'the payload cannot be parsed unambiguously'.format(begins, ends))
		return None

	try:
		results = extract_json(proc.stdout)
	except ValueError as exc:
		report.fail('payload parses as JSON', str(exc))
		return None
	report.ok('payload parses as JSON', 'via the same extract_json ABQflow uses')

	expected = [t['result_name'] for t in tasks]
	missing = [n for n in expected if n not in results]
	if missing:
		report.fail('every task is present in the results', ', '.join(missing))
	else:
		report.ok('every task is present in the results')

	extra = [n for n in results if n not in expected]
	if extra:
		report.warn('no unexpected keys',
					'results carry keys no task asked for: ' + ', '.join(extra))

	failed = [n for n in expected if results.get(n) is None]
	if failed:
		stderr = (proc.stderr or '').strip()
		report.fail(
			'no task returned None',
			'{0}\nEach None is a task that raised. hookkit logged the reason to '
			'stderr:\n{1}'.format(', '.join(failed), stderr[-1500:] or '(stderr empty)'))
	else:
		report.ok('no task returned None')

	return results


# ---------------------------------------------------------------------------
# Check 5 — field values, inline and sidecar
# ---------------------------------------------------------------------------

class _Collect(logging.Handler):
	"""Captures whatever ``_validate_envelope`` warns about."""

	def __init__(self):
		logging.Handler.__init__(self)
		self.messages = []

	def emit(self, record):
		self.messages.append(record.getMessage())


def check_values(report, results, workdir):
	"""Grade each value the way the consumer side (``load_field``) will."""
	logger = logging.getLogger('ABQflow.check_hook.envelope')
	logger.handlers = []
	logger.propagate = False
	sink = _Collect()
	logger.addHandler(sink)
	logger.setLevel(logging.WARNING)

	for name, value in sorted(results.items()):
		if value is None:
			continue

		if isinstance(value, dict) and value.get('__file__'):
			sink.messages = []
			enriched = AbaqusRunner._validate_envelope(value, workdir, logger)
			if enriched is None:
				report.fail("sidecar '{0}' is valid".format(name),
							'\n'.join(sink.messages) or 'rejected by _validate_envelope')
				continue
			if sink.messages:
				report.warn("sidecar '{0}' is valid".format(name),
							'\n'.join(sink.messages))
			else:
				report.ok("sidecar '{0}' is valid".format(name),
							'{0} shape={1}'.format(enriched['__file__'], enriched.get('shape')))
			_check_csv_numeric(report, name, os.path.join(workdir, value['__file__']))
			continue

		if isinstance(value, list):
			bad = _first_non_numeric(value)
			if bad is None:
				report.ok("inline field '{0}' is numeric".format(name),
							'{0} rows'.format(len(value)))
			else:
				report.fail(
					"inline field '{0}' is numeric".format(name),
					"{0!r} will not coerce to float, and load_field() does "
					"np.asarray(value, dtype=float) on inline values — return "
					"strings only through a sidecar (output='file')".format(bad))
			continue

		if isinstance(value, (int, float)):
			report.ok("scalar '{0}'".format(name), value)
		else:
			report.warn("scalar '{0}'".format(name),
						'{0!r} is a {1} — fine as a plain result, but degenerate_from_array '
						'and load_field expect numbers'.format(value, type(value).__name__))


def _first_non_numeric(rows):
	"""Return the first cell that will not become a float, or ``None``."""
	for row in rows:
		cells = row if isinstance(row, (list, tuple)) else [row]
		for cell in cells:
			try:
				float(cell)
			except (TypeError, ValueError):
				return cell
	return None


def _check_csv_numeric(report, name, csv_path):
	"""Warn about CSV columns ``load_field(numeric_only=True)`` would drop."""
	try:
		with open(csv_path, 'r', newline='', encoding='utf-8') as f:
			rows = list(csv.reader(f))
	except OSError as exc:
		report.fail("sidecar CSV '{0}' is readable".format(name), str(exc))
		return
	if len(rows) < 2:
		report.warn("sidecar CSV '{0}' has data".format(name),
					'header only, no rows')
		return

	header, body = rows[0], rows[1:]
	non_numeric = []
	for idx, column in enumerate(header):
		for row in body:
			if idx >= len(row):
				continue
			try:
				float(row[idx])
			except ValueError:
				non_numeric.append(column)
				break
	if non_numeric:
		report.warn("sidecar CSV '{0}' is all-numeric".format(name),
					'load_field(numeric_only=True) drops these columns: ' +
					', '.join(non_numeric))
	else:
		report.ok("sidecar CSV '{0}' is all-numeric".format(name),
					'{0} rows x {1} cols'.format(len(body), len(header)))


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
	parser = argparse.ArgumentParser(
		prog='abqflow-check-hook',
		description='Self-check a hookkit extraction script the way ABQflow will run it.',
		formatter_class=argparse.RawDescriptionHelpFormatter,
		epilog=__doc__.split('Usage\n-----\n', 1)[-1])
	parser.add_argument('hook', help='path to the hook script')

	source = parser.add_mutually_exclusive_group(required=True)
	source.add_argument('--odb', help='ODB artifact (post_extraction, source="odb")')
	source.add_argument('--dat', help='DAT artifact (post_extraction, source="dat")')
	source.add_argument('--inp', help='INP artifact (pre_extraction, CAE kernel)')

	parser.add_argument('--task', action='append', default=[], metavar='NAME[:MODE]',
						help="result_name, optionally with an output mode(inline, file, auto), e.g. "
							"'mises_field:file'. Repeatable.")
	parser.add_argument('--tasks-json', metavar='PATH',
						help='use this tasks JSON instead of --task (supports extra keys)')
	parser.add_argument('--job-name', metavar='NAME',
						help='value for --job_name; defaults to the artifact stem')
	parser.add_argument('--workdir', metavar='DIR',
						help="working directory for the hook. Default: the artifact's "
							"own directory, as in a real run. Use 'temp' for a scratch "
							"dir that is deleted afterwards.")
	parser.add_argument('--abaqus-exe', default=os.environ.get('ABQFLOW_ABAQUS_EXE', 'abaqus'),
						help='Abaqus executable (default: $ABQFLOW_ABAQUS_EXE or "abaqus")')
	parser.add_argument('--no-abqpy', action='store_true',
						help='ignore an installed abqpy and go through the Abaqus '
							'entry point, as a machine without it would')
	parser.add_argument('--timeout', type=float, default=600.0,
						help='seconds before the hook is killed (default: 600)')

	args = parser.parse_args(argv)
	if args.tasks_json and args.task:
		parser.error('--task and --tasks-json are mutually exclusive')
	if not args.tasks_json and not args.task:
		parser.error('give at least one --task, or a --tasks-json file')
	return args


def main(argv=None) -> int:
	"""Entry point for the ``abqflow-check-hook`` command."""
	args = parse_args(argv)

	for name in ('odb', 'dat', 'inp'):
		artifact = getattr(args, name)
		if artifact:
			kind = KINDS[name]
			break

	report = Report()
	print('=' * 68)
	print('check_hook: {0}  ({1} hook)'.format(os.path.basename(args.hook), kind.name))
	print('=' * 68)

	if not check_script(report, args.hook, kind):
		return _verdict(report)

	if not os.path.isfile(artifact):
		report.fail('{0} artifact exists'.format(kind.name), artifact)
		return _verdict(report)
	report.ok('{0} artifact exists'.format(kind.name), artifact)

	tasks = build_tasks(report, args.task, args.tasks_json)
	if tasks is None:
		return _verdict(report)

	job_name = args.job_name or os.path.splitext(os.path.basename(artifact))[0]
	scratch = None
	if args.workdir == 'temp':
		scratch = tempfile.mkdtemp(prefix='check_hook_')
		workdir = scratch
	elif args.workdir:
		workdir = os.path.abspath(args.workdir)
		if not os.path.isdir(workdir):
			os.makedirs(workdir)
	else:
		workdir = os.path.dirname(os.path.abspath(artifact))

	results = None
	try:
		if not stage(report, workdir, kind):
			return _verdict(report)

		has_abqpy = _check_abqpy_installed() and not args.no_abqpy
		proc = run_hook(report, args.hook, artifact, kind, tasks, workdir,
						job_name, args.abaqus_exe, has_abqpy, args.timeout)
		if proc is None:
			return _verdict(report)

		results = check_output(report, proc, tasks)
		if results is not None:
			check_values(report, results, workdir)

		# Abaqus writes its own environment noise to stderr on every launch
		# (vswhere.exe / vars.bat on Windows), so stderr being non-empty is
		# not itself a symptom.  Only hookkit's own per-task failure log is.
		stderr = (proc.stderr or '').strip()
		if stderr and report.failures == 0:
			complaints = [line for line in stderr.splitlines() if "Task '" in line]
			if complaints:
				report.warn('hook logged no complaints', '\n'.join(complaints))
			else:
				print('\nstderr (informational):')
				for line in stderr[-1000:].splitlines():
					print('  {0}'.format(line))
	finally:
		if scratch:
			shutil.rmtree(scratch, ignore_errors=True)

	if results:
		print('\nresults:')
		print(json.dumps(results, indent=2, default=str)[:2000])

	return _verdict(report)


def _verdict(report) -> int:
	print()
	if report.failures:
		print('FAILED — {0} check(s) failed, {1} warning(s). '
				'Fix these before adding the hook to a HookSpec.'
				.format(report.failures, report.warnings))
		return 1
	if report.warnings:
		print('PASSED with {0} warning(s) — the hook will run inside ABQflow.'
				.format(report.warnings))
	else:
		print('PASSED — the hook will run inside ABQflow.')
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
