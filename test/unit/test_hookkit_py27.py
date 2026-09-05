"""Enforce the Python 2.7 compatibility promise of the staged single-file modules.

``hookkit.py`` and ``datkit.py`` are staged into the job directory and imported
by hooks running under ``abaqus python``.  Abaqus 2022 and earlier ship Python
2.7 as that interpreter, so a Py3-only construct in either file breaks remote
extraction on every older machine — and it fails at *import* time, with a
``SyntaxError`` that says nothing about ABQflow.

This was not hypothetical: an f-string on line 74 made all extraction hooks
fail on an Abaqus 2020 machine while passing on an Abaqus 2024 one.

The scan is an AST walk rather than a regex so it cannot be fooled by a
construct appearing inside a string or comment.  No Python 2.7 interpreter is
required.

Run: pytest test/unit/test_hookkit_py27.py -v
"""

from __future__ import annotations

import ast
import os

import pytest

_SRC = os.path.join(
	os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
	'src', 'ABQflow',
)
HOOKKIT = os.path.join(_SRC, 'hookkit.py')
DATKIT = os.path.join(_SRC, 'datkit.py')

# Every AST test below runs against each of these.
PY27_MODULES = [HOOKKIT, DATKIT]

# Modules Python 2.7 does not have. hookkit must stay stdlib-only *and* old.
_PY3_ONLY_MODULES = {
	'pathlib', 'typing', 'dataclasses', 'enum', 'statistics', 'secrets',
	'asyncio', 'concurrent', 'unittest.mock', 'contextvars', 'zoneinfo',
	'tomllib', 'graphlib',
}


@pytest.fixture(scope='module', params=PY27_MODULES,
				ids=lambda path: os.path.basename(path))
def tree(request) -> ast.AST:
	with open(request.param, 'r', encoding='utf-8') as f:
		return ast.parse(f.read(), filename=request.param)


@pytest.mark.parametrize('path', PY27_MODULES, ids=os.path.basename)
def test_staged_module_exists(path):
	assert os.path.isfile(path), f"{os.path.basename(path)} not found at {path}"


def test_no_f_strings(tree):
	"""f-strings are a SyntaxError under Python 2.7."""
	offenders = [
		node.lineno for node in ast.walk(tree) if isinstance(node, ast.JoinedStr)
	]
	assert not offenders, (
		f"f-strings at line(s) {offenders} — Python 2.7 cannot parse them. "
		"Use '{0}'.format(...) instead."
	)


def test_no_builtin_open_with_encoding(tree):
	"""Py2's builtin open() takes no encoding keyword; io.open does."""
	offenders = []
	for node in ast.walk(tree):
		if not isinstance(node, ast.Call):
			continue
		func = node.func
		is_builtin_open = isinstance(func, ast.Name) and func.id == 'open'
		if not is_builtin_open:
			continue
		if any(kw.arg in ('encoding', 'newline', 'errors') for kw in node.keywords):
			offenders.append(node.lineno)
	assert not offenders, (
		f"builtin open() with an encoding/newline/errors keyword at line(s) "
		f"{offenders} — use io.open() for Python 2.7."
	)


def test_no_py3_only_syntax(tree):
	"""Walrus, f-string, starred-return and friends all break Python 2.7."""
	forbidden = {
		'NamedExpr': 'walrus operator :=',
		'JoinedStr': 'f-string',
		'AsyncFunctionDef': 'async def',
		'Await': 'await',
		'AsyncFor': 'async for',
		'AsyncWith': 'async with',
		'MatchValue': 'match statement',
	}
	offenders = []
	for node in ast.walk(tree):
		name = type(node).__name__
		if name in forbidden:
			offenders.append(f"{forbidden[name]} at line {getattr(node, 'lineno', '?')}")
	assert not offenders, "Python 3 only syntax: " + '; '.join(offenders)


def test_no_annotations(tree):
	"""Function/variable annotations are Python 3 syntax."""
	offenders = []
	for node in ast.walk(tree):
		if isinstance(node, ast.AnnAssign):
			offenders.append(f"variable annotation at line {node.lineno}")
		elif isinstance(node, ast.FunctionDef):
			if node.returns is not None:
				offenders.append(f"return annotation on {node.name}()")
			for arg in list(node.args.args) + list(node.args.kwonlyargs):
				if arg.annotation is not None:
					offenders.append(f"annotated arg '{arg.arg}' in {node.name}()")
	assert not offenders, "Python 3 annotations: " + '; '.join(offenders)


def test_no_py3_only_imports(tree):
	"""These modules must import nothing Python 2.7 lacks."""
	offenders = sorted(_imported_roots(tree) & _PY3_ONLY_MODULES)
	assert not offenders, f"modules missing from Python 2.7: {offenders}"


def test_no_abqflow_imports(tree):
	"""They run inside Abaqus's interpreter, where ABQflow is not installed.

	Staging copies the bare file into the job directory, so any ``import
	ABQflow...`` would raise there while passing every test on this machine.
	"""
	assert 'ABQflow' not in _imported_roots(tree), (
		"a staged module imported ABQflow — it must stay standalone and "
		"stdlib-only; duplicate the constant instead, as hookkit does for the "
		"result sentinels."
	)


def _imported_roots(tree: ast.AST) -> set[str]:
	imported = set()
	for node in ast.walk(tree):
		if isinstance(node, ast.Import):
			imported.update(alias.name.split('.')[0] for alias in node.names)
		elif isinstance(node, ast.ImportFrom) and node.module:
			imported.add(node.module.split('.')[0])
	return imported


def test_classes_are_new_style(tree):
	"""Under Py2, ``class Foo:`` is old-style; ``with`` needs new-style."""
	offenders = [
		node.name for node in ast.walk(tree)
		if isinstance(node, ast.ClassDef) and not node.bases
	]
	assert not offenders, (
		f"classes without an explicit base: {offenders} — "
		"write 'class Foo(object):' so they are new-style under Python 2.7."
	)


def test_text_helper_exists():
	"""Py2 needs a unicode/str shim for io.open text streams.

	Without it ``_write_csv`` raises "write() argument 1 must be unicode, not
	str" under Python 2.7 — observed on an Abaqus 2020 machine, where it
	turned every CSV sidecar result into ``None`` instead of failing loudly.
	"""
	from ABQflow import hookkit

	assert hasattr(hookkit, '_text'), (
		"hookkit._text is missing — values written to an io.open(encoding=...) "
		"stream must go through it, or Python 2.7 rejects them."
	)


def test_write_csv_round_trip(tmp_path):
	"""_write_csv produces a header plus one line per row."""
	import csv

	from ABQflow import hookkit

	target = tmp_path / 'out.csv'
	rows = [[1, 2.5], [3, 4.5]]
	hookkit._write_csv(rows, ['label', 'value'], str(target))

	with open(target, newline='') as f:
		parsed = list(csv.reader(f))

	assert parsed[0] == ['label', 'value']
	assert len(parsed) == 3
	assert parsed[1] == ['1', '2.5']


def test_sentinels_match_the_package_constants():
	"""hookkit deliberately re-declares the sentinels; they must not drift.

	hookkit never imports ABQflow (it runs inside Abaqus's interpreter, where
	the package is not installed), so the protocol constants exist twice.
	"""
	from ABQflow.helpers.constant import RESULT_BEGIN, RESULT_END

	namespace: dict = {}
	with open(HOOKKIT, 'r', encoding='utf-8') as f:
		for line in f:
			if line.startswith(('RESULT_BEGIN', 'RESULT_END')):
				exec(line, namespace)  # noqa: S102 - two literal assignments

	assert namespace['RESULT_BEGIN'] == RESULT_BEGIN
	assert namespace['RESULT_END'] == RESULT_END
