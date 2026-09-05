# -*- coding: utf-8 -*-
"""datkit — parser for Abaqus ``.dat`` printed output.

Single file, stdlib only, Py2.7 / Py3 compatible — the same contract as
:mod:`ABQflow.hookkit`, and for the same reason: this file is staged into the
job directory and imported by hook scripts, which may run under an interpreter
that is not the one ABQflow itself runs on.  It never imports ABQflow.

Why this exists
---------------
An ``.odb`` is often too large to open just to read a handful of numbers.  The
usual workaround is to add ``*EL PRINT`` / ``*NODE PRINT`` to the deck so that
Abaqus also writes the values as text into ``<job>.dat``.  This module turns
that text back into rows and columns.

What it parses
--------------
The ``.dat`` is ~99% input echo followed by, per converged increment, an
``INCREMENT n SUMMARY`` block and one printed table per output request.  A
table is a banner (``N O D E   O U T P U T``), an optional "THE FOLLOWING
TABLE IS PRINTED FOR ..." line naming the set, a two-line header whose
``FOOT-`` / ``NOTE`` heading is split across both lines, the data rows, and
``MAXIMUM`` / ``MINIMUM`` / ``AT NODE`` summary rows.  Those land in
``table['summary']``, where the ``AT NODE`` rows are keyed ``'MAXIMUM AT'`` /
``'MINIMUM AT'`` after the row each one annotates.

Usage::

	import datkit

	doc = datkit.parse(dat_path)
	tables = datkit.select_tables(doc, kind='node', set_name='*INSPECTION*',
	                              increment='last')
	header, rows = datkit.to_rows(tables, columns=['U2'])

For a file too large to hold, or when only one increment is wanted::

	doc = datkit.parse(dat_path, increments='last')   # streams, drops the rest
	for table in datkit.iter_tables(dat_path, kinds='node'):  # retains nothing
		...

.. important::
   The Py2.7 half of the promise is load-bearing — no f-strings, no
   annotations, no ``open(..., encoding=...)``, every class explicitly
   ``(object)``.  ``test/unit/test_hookkit_py27.py`` enforces it with an AST
   scan over this file as well as hookkit.py; run it before changing anything
   here.
"""


import fnmatch
import io
import os
import re
from collections import OrderedDict


DATKIT_VERSION = '1.0'


# ---------------------------------------------------------------------------
# Format constants
# ---------------------------------------------------------------------------

# Banner text (after _despace) -> table kind.  The despaced form is what makes
# 'E L E M E N T   Q U A L I T Y  C H E C K S' — which lives in the echo
# section and is not a table — impossible to confuse with element output.
_BANNERS = {
	'NODE OUTPUT':     'node',
	'ELEMENT OUTPUT':  'element',
	'ENERGY OUTPUT':   'energy',
	'ENERGY TOTALS':   'energy',
	'CONTACT OUTPUT':  'contact',
	'SECTION OUTPUT':  'section',
	'SURFACE OUTPUT':  'surface',
	'MODAL OUTPUT':    'modal',
}

# 'INCREMENT' sits at 0-based column 32 in every file seen; \s{16,} keeps that
# tolerant of column drift while still excluding echoed INP lines, which carry
# a line number in the first columns.
_INC_RE        = re.compile(r'^\s{16,}INCREMENT\s+(\d+)\s+SUMMARY\s*$')
# The spaced form is the step *title*; the unspaced 'STEP n INCREMENT m' is the
# page banner, matched separately.
_STEP_TITLE_RE = re.compile(r'^\s+S T E P\s+(\d+)\s+(\S.*?)\s*$')
_PAGE_RE       = re.compile(r'^\s+STEP\s+(\d+)\s+INCREMENT\s+(\d+)\s*$')
_SET_RE        = re.compile(r'\b(?:NODE|ELEMENT)\s+SET\s+(\S+)', re.IGNORECASE)
_META_RE       = re.compile(
	r'^\s*([A-Z][A-Z0-9 /\-]*[A-Z0-9])\s{2,}'
	r'([-+]?[0-9][0-9.]*(?:[EeDd][-+]?[0-9]+)?)\s*$')
# Fortran drops the 'E' when the exponent needs three digits: 4.2961278-303.
_EXP_FIX_RE    = re.compile(r'^([-+]?[0-9]*\.?[0-9]+)([-+][0-9]{2,3})$')

_ECHO_END  = 'END OF USER INPUT PROCESSING'
_TERM_OK   = 'THE ANALYSIS HAS BEEN COMPLETED'
_TERM_INC  = 'THE NUMBER OF INCS ON THE *STEP CARD HAS BEEN COMPLETED'
_DESC_MARK = 'THE FOLLOWING TABLE IS PRINTED'

# Header tokens that identify a row *label* rather than a printed value.
_LABEL_TOKENS = ('NODE', 'ELEMENT', 'ELEM', 'PT', 'SP', 'IP', 'SEC', 'FOOTNOTE')
# Longest first, so 'AT ELEMENT' wins over 'ELEMENT' and 'AT NODE' over 'NODE'.
_SUMMARY_LABELS = ('AT ELEMENT', 'AT NODE', 'MAXIMUM', 'MINIMUM',
					'AVERAGE', 'ELEMENT', 'TOTAL', 'NODE', 'RMS')
# Locator rows: they carry labels, not values.  Node output writes 'AT NODE';
# element output writes a bare 'ELEMENT' under each MAXIMUM/MINIMUM, which is
# also how a re-printed column header starts — hence the all-integers guard at
# the use site.
_INT_SUMMARY = ('AT ELEMENT', 'AT NODE', 'ELEMENT', 'NODE')

# Canonical names for metadata whose printed label carries a filler word.
# The printed name is replaced, not supplemented.
_META_ALIASES = {
	'current_load_proportionality_factor': 'load_proportionality_factor',
}

_MAX_RAW_LINES = 200

_INF = float('inf')

# Py2's ``str`` is bytes and its text type is ``unicode``; a caller passing a
# ``u'node'`` literal there must not fall through the isinstance checks below.
try:                       # pragma: no cover - Python 2 only
	_string_types = (str, unicode)   # noqa: F821 - undefined on Py3 by design
except NameError:          # pragma: no cover - Python 3
	_string_types = (str,)

# Parser states
_ECHO, _BODY, _AWAIT, _AWAIT_NOTE, _IN_TABLE, _RAW = 0, 1, 2, 3, 4, 5


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------

def to_float(token):
	"""Parse one printed value, returning ``None`` rather than raising.

	Handles plain floats, Fortran ``D`` exponents (``1.0D-03``), the
	missing-``E`` three-digit exponent form (``4.2961278-303``), ``***``
	overflow fields, and NaN/Inf — all of which appear in real ``.dat``
	files and none of which should kill a whole table.
	"""
	if token is None:
		return None
	s = str(token).strip()
	if not s or '*' in s:
		return None
	value = _try_float(s)
	if value is not None:
		return value
	swapped = s.replace('D', 'E').replace('d', 'e')
	value = _try_float(swapped)
	if value is not None:
		return value
	match = _EXP_FIX_RE.match(swapped)
	if match is not None:
		return _try_float(match.group(1) + 'E' + match.group(2))
	return None


def _try_float(text):
	try:
		value = float(text)
	except (ValueError, TypeError):
		return None
	if value != value or value == _INF or value == -_INF:
		return None   # NaN / Inf carry no information a caller can use
	return value


def _to_int(token):
	"""Integer label if it parses as one, otherwise the token unchanged."""
	try:
		return int(token)
	except (ValueError, TypeError):
		return token


def _looks_int(token):
	try:
		int(token)
		return True
	except (ValueError, TypeError):
		return False


def _despace(text):
	"""Collapse Abaqus's letter-spaced headings: 'N O D E   O U T P U T' -> 'NODE OUTPUT'.

	Words are separated by two or more spaces and letters by one, so splitting
	on ``\\s{2,}`` and squeezing each group recovers the original words.
	"""
	parts = re.split(r'\s{2,}', text.strip())
	return ' '.join(''.join(part.split()) for part in parts)


def match_set(table_set, pattern):
	"""Case-insensitive :mod:`fnmatch` of a table's set name against *pattern*.

	``.dat`` upper-cases set names relative to the INP, so an exact name from
	the deck still matches.  ``None`` *pattern* matches everything; a table
	with no set never matches a non-``None`` pattern.
	"""
	if pattern is None:
		return True
	if table_set is None:
		return False
	return fnmatch.fnmatch(str(table_set).upper(), str(pattern).upper())


# ---------------------------------------------------------------------------
# Line classification
# ---------------------------------------------------------------------------

def _is_blank(line):
	"""True for both the truly empty and the exactly-two-spaces separators."""
	return not line.strip()


def _is_noise(line):
	"""Page-break furniture that may interrupt a table without ending it."""
	stripped = line.strip()
	if stripped == '1':
		return True
	if stripped.startswith('Abaqus') and 'Date' in stripped:
		return True
	if stripped.startswith('For use by'):
		return True
	if 'TIME COMPLETED IN THIS STEP' in stripped:
		return True
	return _PAGE_RE.match(line) is not None


def _banner_kind(line):
	"""Table kind for an output banner line, or ``None``."""
	if ' O U T P U T' not in line and ' T O T A L S' not in line:
		return None
	return _BANNERS.get(_despace(line))


def _is_increment(line):
	return 'SUMMARY' in line and _INC_RE.match(line) is not None


def _is_step_title(line):
	return 'S T E P' in line and _STEP_TITLE_RE.match(line) is not None


def _is_terminator(line):
	return _TERM_OK in line or _TERM_INC in line


def _summary_label(stripped):
	"""The summary-row label at the start of *stripped*, or ``None``."""
	for label in _SUMMARY_LABELS:
		if stripped.startswith(label):
			rest = stripped[len(label):]
			if not rest or rest[0] == ' ':
				return label
	return None


def _has_number(tokens):
	for token in tokens:
		if to_float(token) is not None:
			return True
	return False


def _all_ints(tokens):
	"""True when *tokens* is a non-empty run of integers — a locator row."""
	if not tokens:
		return False
	for token in tokens:
		if not _looks_int(token):
			return False
	return True


def _classify(columns):
	"""Split a merged header into label columns, value columns, footnote index."""
	labels = []
	footnote_index = None
	i = 0
	while i < len(columns) and columns[i] in _LABEL_TOKENS:
		if columns[i] == 'FOOTNOTE':
			footnote_index = i
		else:
			labels.append(columns[i])
		i += 1
	return labels, list(columns[i:]), footnote_index


def _merge_header(tokens, continuation):
	"""Join hyphen-ended header tokens with their continuation line.

	``['NODE', 'FOOT-', 'U1', ...]`` + ``['NOTE']`` -> ``['NODE', 'FOOTNOTE',
	'U1', ...]``.  A generic rule, applied in order, so any split heading is
	rejoined — not just ``FOOT-NOTE``.  Returns ``(tokens, consumed)``.
	"""
	hyphenated = [t for t in tokens if t.endswith('-')]
	if not hyphenated or len(continuation) != len(hyphenated):
		return tokens, False
	if _has_number(continuation):
		return tokens, False
	merged = []
	index = 0
	for token in tokens:
		if token.endswith('-'):
			merged.append(token[:-1] + continuation[index])
			index += 1
		else:
			merged.append(token)
	return merged, True


def _parse_meta_line(line, meta):
	"""Harvest ``LABEL␣␣VALUE`` pairs from an increment/step summary line.

	Deliberately generic: Riks prints ``CURRENT LOAD PROPORTIONALITY FACTOR``
	and ``TOTAL ARC LENGTH ..., INCREMENT OF ARC LENGTH ...`` while a general
	static step prints ``STEP TIME COMPLETED`` / ``TOTAL TIME COMPLETED``, and
	one regex covers all of them without a per-procedure branch.
	"""
	for fragment in line.split(','):
		match = _META_RE.match(fragment)
		if match is None:
			continue
		key = match.group(1).strip().lower().replace(' ', '_').replace('-', '_')
		# Store the canonical name only — keeping both would show up as two
		# identical columns the moment someone asks for the whole meta table.
		meta[_META_ALIASES.get(key, key)] = to_float(match.group(2))


# ---------------------------------------------------------------------------
# Streaming event parser
# ---------------------------------------------------------------------------

def _new_table(kind, title, lineno):
	return {
		'kind': kind, 'title': title, 'set': None, 'description': None,
		'columns': [], 'label_columns': [], 'value_columns': [],
		'footnote_index': None, 'rows': [], 'labels': [], 'summary': {},
		'step': None, 'increment': None, 'meta': {}, 'line': lineno,
		'truncated': False,
	}


def _wanted(kind, set_name_pattern, table, steps):
	if kind is not None:
		kinds = (kind,) if isinstance(kind, _string_types) else tuple(kind)
		if table['kind'] not in kinds:
			return False
	if not match_set(table['set'], set_name_pattern):
		return False
	if steps is not None:
		wanted = steps if isinstance(steps, (list, tuple, set)) else (steps,)
		if table['step'] not in wanted:
			return False
	return True


def _build_row(parts, table):
	"""Turn a split data line into a row aligned to ``table['columns']``.

	Returns ``(row, warning_or_None)``.  Arity mismatches pad or truncate and
	report rather than raising — one malformed line must not lose the table.
	"""
	footnote_index = table['footnote_index']
	n_lead = footnote_index if footnote_index is not None else len(table['label_columns'])
	n_value = len(table['value_columns'])

	lead = [_to_int(p) for p in parts[:n_lead]]
	rest = parts[n_lead:]
	footnote = ''
	warning = None

	if footnote_index is not None and len(rest) == n_value + 1:
		footnote = rest[0]
		rest = rest[1:]
	elif len(rest) != n_value:
		warning = ("line {0}: {1} value token(s) for {2} column(s) in "
					"{3} table".format(table['line'], len(rest), n_value, table['kind']))

	values = [to_float(v) for v in rest[:n_value]]
	while len(values) < n_value:
		values.append(None)
	while len(lead) < n_lead:
		lead.append(None)

	row = list(lead)
	if footnote_index is not None:
		row.append(footnote)
	row.extend(values)
	return row, warning


def _is_data_row(table, parts):
	if not parts:
		return False
	if table['label_columns']:
		return _looks_int(parts[0])
	return to_float(parts[0]) is not None


def _iter_events(path, kinds=None, set_name=None, steps=None, max_rows=None):
	"""Single-pass state machine yielding ``(event, payload)`` tuples.

	Events: ``'step'``, ``'increment'``, ``'table'``, ``'warning'``,
	``'status'``.  Step and increment payloads are yielded when they *open*,
	with empty ``meta``; the parser keeps mutating the same dict, so by the
	time a table from that increment is yielded its metadata is complete.

	Retains nothing beyond the table currently being built, which is what
	makes an 84k-line file cost the same as a 100-line one.
	"""
	if not os.path.isfile(path):
		raise IOError("dat file not found: {0}".format(path))

	state = _ECHO
	step = None
	increment = None
	table = None          # the open table
	opening = None        # table descriptor being built in _AWAIT / _AWAIT_NOTE
	header_tokens = None
	resume = False        # the open table may continue after a page break
	describing = False    # inside a (possibly wrapped) description line
	last_summary = None   # summary row an 'AT NODE' line belongs to
	pending_meta = False
	banner_step = None
	banner_increment = None
	status = {'completed': False, 'terminator': None}
	lineno = 0

	stream = io.open(path, 'r', encoding='utf-8', errors='replace')
	try:
		for raw in stream:
			lineno += 1
			line = raw.rstrip('\n').rstrip('\r')

			handled = False
			while not handled:
				handled = True

				# ---- echo section: 99% of the file, three cheap tests ----
				if state == _ECHO:
					if _ECHO_END in line:
						state = _BODY
					elif _is_increment(line) or _is_step_title(line):
						state = _BODY
						handled = False   # re-dispatch in _BODY

				# ---- between tables ----
				elif state == _BODY:
					if _is_increment(line):
						number = int(_INC_RE.match(line).group(1))
						increment = {
							'step': step['step'] if step else (banner_step or 1),
							'increment': number, 'meta': {}, 'tables': [],
						}
						pending_meta = True
						yield ('increment', increment)
					elif _banner_kind(line) is not None:
						pending_meta = False
						opening = _new_table(_banner_kind(line), _despace(line), lineno)
						state = _AWAIT
					elif _is_step_title(line):
						match = _STEP_TITLE_RE.match(line)
						step = {'step': int(match.group(1)),
								'title': _despace(match.group(2)),
								'meta': {}, 'increments': []}
						increment = None
						pending_meta = True
						yield ('step', step)
					elif _PAGE_RE.match(line) is not None:
						page = _PAGE_RE.match(line)
						banner_step = int(page.group(1))
						banner_increment = int(page.group(2))
					elif _TERM_OK in line:
						status['completed'] = True
						status['terminator'] = 'completed'
					elif _TERM_INC in line:
						status['terminator'] = 'inc_limit'
					elif pending_meta:
						target = increment if increment is not None else step
						if target is not None:
							_parse_meta_line(line, target['meta'])

				# ---- banner seen, looking for the description / header ----
				elif state == _AWAIT:
					if _is_blank(line) or _is_noise(line):
						describing = False   # a blank ends the description
					elif _DESC_MARK in line.upper():
						opening['description'] = line.strip()
						describing = True
					elif describing:
						# Abaqus wraps this line at the page width, so the set
						# name routinely lands on the next line by itself:
						#   ... AND ELEMENT SET
						#   ASSEMBLY_ALL
						# Reading only the first line loses the set entirely.
						opening['description'] += ' ' + line.strip()
					elif (_is_increment(line) or _is_step_title(line)
							or _is_terminator(line)):
						# The banner never produced a table.
						if table is not None:
							yield ('table', table)
							table = None
						opening = None
						resume = False
						state = _BODY
						handled = False
					else:
						tokens = line.split()
						if _has_number(tokens):
							# No recognisable header — keep the text verbatim
							# rather than guessing, and never raise.
							if table is not None:
								yield ('table', table)
								table = None
							opening['kind'] = 'raw'
							opening['lines'] = [line]
							table = opening
							opening = None
							resume = False
							state = _RAW
						else:
							header_tokens = [t.upper() for t in tokens]
							state = _AWAIT_NOTE

				# ---- the line after a header may carry its 'NOTE' half ----
				elif state == _AWAIT_NOTE:
					describing = False
					# Resolve the set from the whole description, wrapped or not.
					if opening is not None and opening['description']:
						found = _SET_RE.search(opening['description'])
						opening['set'] = found.group(1) if found else None
					header_tokens, consumed = _merge_header(header_tokens, line.split())
					columns = header_tokens
					labels, values, footnote_index = _classify(columns)

					# A page break re-prints the banner and the column header but
					# NOT the "THE FOLLOWING TABLE IS PRINTED FOR ..." line, so
					# a missing description is what distinguishes a continuation
					# from a genuinely new table.  That matters for *EL PRINT*
					# over a mixed mesh, where Abaqus emits one table per element
					# type — same kind, same set, same columns — and merging them
					# would collapse two summaries into one.
					same = (resume and table is not None
							and opening['description'] is None
							and table['kind'] == opening['kind']
							and table['columns'] == columns)
					if same:
						opening = None            # continuation of the open table
					else:
						if table is not None:
							yield ('table', table)
							table = None
						opening['columns'] = columns
						opening['label_columns'] = labels
						opening['value_columns'] = values
						opening['footnote_index'] = footnote_index
						opening['step'] = step['step'] if step else (banner_step or 1)
						opening['increment'] = (increment['increment'] if increment
												else (banner_increment or 0))
						opening['meta'] = (increment['meta'] if increment
											else (step['meta'] if step else {}))
						opening['_keep'] = _wanted(kinds, set_name, opening, steps)
						table = opening
						opening = None
					resume = False
					last_summary = None
					header_tokens = None
					state = _IN_TABLE
					handled = consumed   # if not consumed, this line is a data row

				# ---- collecting rows ----
				elif state == _IN_TABLE:
					if _is_blank(line) or _is_noise(line):
						pass   # a blank line does NOT close a table
					elif (_is_increment(line) or _is_step_title(line)
							or _is_terminator(line)):
						yield ('table', table)
						table = None
						state = _BODY
						handled = False
					elif _banner_kind(line) is not None:
						kind = _banner_kind(line)
						if kind == table['kind']:
							# Page break mid-table: same kind, header re-printed.
							opening = _new_table(kind, _despace(line), lineno)
							resume = True
							state = _AWAIT
						else:
							yield ('table', table)
							table = None
							state = _BODY
							handled = False
					elif _DESC_MARK in line.upper():
						# A new table with no banner of its own.  ``*EL PRINT``
						# over a mixed mesh prints one table per element type but
						# only one ``E L E M E N T   O U T P U T`` heading, so
						# every table after the first begins right here.  Without
						# this the second type's rows are appended to the first
						# table and its summary overwrites the first's.
						yield ('table', table)
						opening = _new_table(table['kind'], table['title'], lineno)
						table = None
						resume = False
						state = _AWAIT
						handled = False   # re-dispatch so _AWAIT reads it
					else:
						stripped = line.strip()
						label = _summary_label(stripped)
						tokens = stripped[len(label):].split() if label else []
						if label in _INT_SUMMARY and _all_ints(tokens):
							# The locator row is printed once under MAXIMUM and
							# again under MINIMUM ('AT NODE' for node output, a
							# bare 'ELEMENT' for element output).  Key it by the
							# row it annotates, or the second silently replaces
							# the first.  The all-integers test is what keeps a
							# re-printed 'ELEMENT  PT  MISES' column header from
							# being mistaken for one.
							key = (last_summary + ' AT') if last_summary else label
							table['summary'][key] = [_to_int(t) for t in tokens]
						elif label is not None and label not in _INT_SUMMARY:
							table['summary'][label] = [to_float(t) for t in tokens]
							last_summary = label
						else:
							parts = stripped.split()
							if _is_data_row(table, parts):
								row, warning = _build_row(parts, table)
								if warning is not None:
									yield ('warning', warning)
								if table['_keep']:
									if max_rows is not None and len(table['rows']) >= max_rows:
										table['truncated'] = True
									else:
										table['rows'].append(row)
										table['labels'].append(row[0] if row else None)

				# ---- unrecognised layout, captured verbatim ----
				else:   # _RAW
					if (_is_increment(line) or _is_step_title(line)
							or _is_terminator(line) or _banner_kind(line) is not None):
						while table['lines'] and _is_blank(table['lines'][-1]):
							table['lines'].pop()
						yield ('table', table)
						table = None
						state = _BODY
						handled = False
					elif len(table['lines']) < _MAX_RAW_LINES:
						table['lines'].append(line)
					else:
						table['truncated'] = True

		if table is not None:
			yield ('table', table)
		yield ('status', status)
	finally:
		stream.close()


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def iter_tables(path, kinds=None, set_name=None, steps=None, max_rows=None):
	"""Yield tables one at a time, retaining nothing between them.

	Use this instead of :func:`parse` when the file is large and the caller
	only needs to fold over the tables.  *steps* accepts an int or a sequence
	of ints — ``'last'`` cannot be resolved while streaming, so ask
	:func:`parse` for that.
	"""
	for event, payload in _iter_events(path, kinds=kinds, set_name=set_name,
										steps=steps, max_rows=max_rows):
		if event == 'table' and payload.get('_keep', True):
			payload.pop('_keep', None)
			yield payload


def parse(path, kinds=None, set_name=None, steps=None, increments='all',
			keep_rows=True, max_rows=None):
	"""Parse *path* into a document dict.

	Parameters
	----------
	path : str
		Path to the ``.dat`` file.
	kinds : str or sequence of str or None
		Keep only these table kinds (``'node'``, ``'element'``, ``'energy'``,
		``'contact'``, ...).
	set_name : str or None
		Keep only tables whose set matches this :mod:`fnmatch` pattern
		(case-insensitive).
	steps : int or sequence of int or None
		Keep only tables from these steps.
	increments : str or int or sequence
		``'all'`` (default), ``'first'``, ``'last'``, a positive increment
		number, a negative index from the end, or a sequence.  ``'first'``,
		``'last'``, positive numbers and negative indices are applied
		*while streaming*, so peak memory stays at one increment however
		long the analysis ran.
	keep_rows : bool
		``False`` discards row data, keeping only ``summary`` and ``shape``.
	max_rows : int or None
		Cap the rows retained per table; capped tables carry
		``truncated=True``.

	Returns
	-------
	dict
		``{'path', 'completed', 'terminator', 'steps', 'increments',
		'increment_count', 'table_count', 'warnings', ...}``.
		``increment_count`` and ``table_count`` describe the *file* — every
		increment, and every table passing *kinds* / *set_name* / *steps* —
		not the subset *increments* retained, so a log line built from them
		still says how big the run was.
	"""
	doc = {
		'path': os.path.abspath(path), 'datkit_version': DATKIT_VERSION,
		'completed': False, 'terminator': None,
		'steps': [], 'increments': [],
		'increment_count': 0, 'table_count': 0, 'warnings': [],
	}
	mode, window = _increment_mode(increments)
	current = None      # the increment tables are currently attaching to
	dropped = False     # an increment is open but was filtered out
	orphans = []        # tables printed before any INCREMENT SUMMARY

	for event, payload in _iter_events(path, kinds=kinds, set_name=set_name,
										steps=steps, max_rows=max_rows):
		if event == 'step':
			doc['steps'].append(payload)

		elif event == 'increment':
			doc['increment_count'] += 1
			if mode == 'first':
				keep = (doc['increment_count'] == 1)
			elif mode == 'number':
				keep = payload['increment'] in window
			else:
				keep = True
			dropped = not keep
			if keep:
				doc['increments'].append(payload)
				current = payload
				if mode == 'window' and len(doc['increments']) > window:
					doc['increments'].pop(0)
			else:
				current = None

		elif event == 'table':
			if not payload.pop('_keep', True):
				continue
			doc['table_count'] += 1
			if not keep_rows:
				payload['shape'] = [len(payload['rows']), len(payload['columns'])]
				payload['rows'] = []
				payload['labels'] = []
			if current is not None:
				current['tables'].append(payload)
			elif not dropped:
				# No increment has opened at all — see _synthesise_increments.
				# A table whose increment *was* opened and then filtered out is
				# discarded, not resurrected as an orphan.
				orphans.append(payload)

		elif event == 'warning':
			doc['warnings'].append(payload)

		elif event == 'status':
			doc['completed'] = payload['completed']
			doc['terminator'] = payload['terminator']

	# Output printed with no INCREMENT SUMMARY (linear perturbation, frequency)
	# still belongs somewhere — synthesise the increment its banner named.
	if orphans:
		synthesised = _synthesise_increments(orphans)
		doc['increments'].extend(synthesised)
		doc['increment_count'] += len(synthesised)
		doc['increments'].sort(key=lambda i: (i['step'], i['increment']))

	if mode == 'defer':
		doc['increments'] = select_increments(doc, increment=increments)
	elif mode == 'window' and window > 1:
		# A negative selector retained a rolling window of ``window`` entries,
		# so the one asked for is at its front — and only exists if the window
		# ever filled.
		doc['increments'] = (doc['increments'][:1]
								if len(doc['increments']) == window else [])

	kept = doc['increments']
	for step in doc['steps']:
		step['increments'] = [i for i in kept if i['step'] == step['step']]
	return doc


def scan_status(path, tail_bytes=65536):
	"""Read only the tail of *path* and report how the analysis ended.

	``ANALYSIS COMPLETE`` is printed whether or not the step converged, so it
	is deliberately not consulted: ``completed`` follows ``THE ANALYSIS HAS
	BEEN COMPLETED`` alone.
	"""
	if not os.path.isfile(path):
		raise IOError("dat file not found: {0}".format(path))
	size = os.path.getsize(path)
	handle = io.open(path, 'rb')
	try:
		if size > tail_bytes:
			handle.seek(size - tail_bytes)
		blob = handle.read().decode('utf-8', 'replace')
	finally:
		handle.close()
	if _TERM_OK in blob:
		return {'completed': True, 'terminator': 'completed'}
	if _TERM_INC in blob:
		return {'completed': False, 'terminator': 'inc_limit'}
	return {'completed': False, 'terminator': None}


def _increment_mode(increments):
	"""Map an increment selector onto a streaming retention policy."""
	if increments is None or increments == 'all':
		return 'all', None
	if increments == 'first':
		return 'first', None
	if increments == 'last':
		return 'window', 1
	if isinstance(increments, bool):
		return 'defer', None
	if isinstance(increments, int):
		if increments > 0:
			return 'number', (increments,)
		return 'window', abs(increments)
	if isinstance(increments, (list, tuple, set)):
		numbers = tuple(n for n in increments if isinstance(n, int) and n > 0)
		if len(numbers) == len(tuple(increments)):
			return 'number', numbers
	return 'defer', None


def _synthesise_increments(tables):
	"""Group tables that arrived with no INCREMENT SUMMARY into pseudo-increments."""
	buckets = OrderedDict()
	for table in tables:
		key = (table['step'], table['increment'])
		if key not in buckets:
			buckets[key] = {'step': key[0], 'increment': key[1],
							'meta': table.get('meta') or {}, 'tables': []}
		buckets[key]['tables'].append(table)
	return list(buckets.values())


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------

def select_increments(doc, step=None, increment='last'):
	"""Pick increments out of a parsed *doc*.

	*step* is ``None`` (all), an int, ``'last'``, or a sequence.
	*increment* is ``'all'``, ``'first'``, ``'last'``, a positive increment
	number, a negative index from the end, or a sequence of either.
	"""
	found = list(doc.get('increments') or [])

	if step is not None:
		if step == 'last':
			numbers = [i['step'] for i in found]
			step = max(numbers) if numbers else None
		if step is not None:
			wanted = step if isinstance(step, (list, tuple, set)) else (step,)
			found = [i for i in found if i['step'] in wanted]

	if increment is None or increment == 'all':
		return found
	if increment == 'last':
		return found[-1:]
	if increment == 'first':
		return found[:1]
	if isinstance(increment, int):
		return _one_increment(found, increment)
	if isinstance(increment, (list, tuple, set)):
		picked = []
		for item in increment:
			for candidate in _one_increment(found, item):
				if candidate not in picked:
					picked.append(candidate)
		return [i for i in found if i in picked]
	raise ValueError("unsupported increment selector: {0!r}".format(increment))


def _one_increment(found, number):
	if not isinstance(number, int):
		raise ValueError("increment must be an int, got {0!r}".format(number))
	if number < 0:
		return [found[number]] if len(found) >= -number else []
	return [i for i in found if i['increment'] == number]


def select_tables(doc, kind=None, set_name=None, step=None, increment='last'):
	"""Pick tables out of a parsed *doc*, filtered by kind and set name."""
	picked = []
	for inc in select_increments(doc, step=step, increment=increment):
		for table in inc['tables']:
			if kind is not None and table['kind'] != kind:
				continue
			if set_name is not None and not match_set(table['set'], set_name):
				continue
			picked.append(table)
	return picked


# ---------------------------------------------------------------------------
# Shaping
# ---------------------------------------------------------------------------

def _select_columns(table, columns):
	available = table['value_columns']
	if columns is None:
		return list(available)
	upper = dict((c.upper(), c) for c in available)
	chosen = []
	for name in columns:
		key = str(name).upper()
		if key in upper:
			chosen.append(upper[key])
	return chosen


def _index_value(table, key):
	name = str(key)
	if name in ('step', 'increment', 'set', 'kind', 'title', 'line'):
		return table.get(name)
	meta = table.get('meta') or {}
	return meta.get(name.lower().replace(' ', '_'))


def to_rows(tables, columns=None, labels=None, index_columns=None,
			include_labels=True, include_footnote=False):
	"""Flatten *tables* into a single ``(header, rows)`` pair.

	Parameters
	----------
	tables : list[dict]
		Tables from :func:`select_tables` or :func:`iter_tables`.
	columns : list[str] or None
		Value columns to keep, by name (case-insensitive).  ``None`` keeps all.
	labels : sequence or None
		Keep only rows whose node/element label is in this set.
	index_columns : list[str] or None
		Columns prepended from each table's context — ``'step'``,
		``'increment'``, or any increment-metadata key such as
		``'load_proportionality_factor'``.
	include_labels : bool
		Prepend the label columns (``NODE``, or ``ELEMENT`` + ``PT``).
	include_footnote : bool
		Keep the footnote column.  Off by default: it holds text, and
		:func:`ABQflow.helpers.convert.load_field` expects every cell of a
		field to be numeric.
	"""
	usable = [t for t in tables if t.get('columns')]
	if not usable:
		return [], []

	index_columns = list(index_columns or [])
	first = usable[0]
	header = list(index_columns)
	if include_labels:
		header.extend(first['label_columns'])
	if include_footnote and first.get('footnote_index') is not None:
		header.append('FOOTNOTE')
	header.extend(_select_columns(first, columns))

	wanted_labels = set(labels) if labels is not None else None
	rows = []
	for table in usable:
		position = dict((name, i) for i, name in enumerate(table['columns']))
		chosen = _select_columns(table, columns)
		n_labels = len(table['label_columns'])
		footnote_index = table.get('footnote_index')
		prefix = [_index_value(table, key) for key in index_columns]
		for row in table['rows']:
			if wanted_labels is not None and (not row or row[0] not in wanted_labels):
				continue
			out = list(prefix)
			if include_labels:
				out.extend(row[:n_labels])
			if include_footnote and footnote_index is not None:
				out.append(row[footnote_index])
			for name in chosen:
				index = position.get(name)
				out.append(row[index] if index is not None and index < len(row) else None)
			rows.append(out)
	return header, rows


def column_values(table, name, labels=None):
	"""Every value of one column of one table, in row order."""
	_header, rows = to_rows([table], columns=[name], labels=labels,
							include_labels=False)
	return [row[0] for row in rows] if rows else []


def summary(table, row='MAXIMUM', columns=None):
	"""One summary row of *table* as a ``{column: value}`` mapping.

	*row* is ``'MAXIMUM'``, ``'MINIMUM'``, ``'TOTAL'``, ``'AVERAGE'``, ... or
	one of the label rows: Abaqus prints ``AT NODE`` (or ``AT ELEMENT``) twice,
	once under ``MAXIMUM`` and once under ``MINIMUM``, so each is keyed by the
	row it belongs to — ``'MAXIMUM AT'`` and ``'MINIMUM AT'`` — and asking for
	``'MAXIMUM AT'`` tells you *where* each column peaked.

	Returns an :class:`~collections.OrderedDict` so callers get a stable
	column order under Python 2.7 as well.  An unknown *row* gives ``{}``.
	"""
	values = (table.get('summary') or {}).get(str(row).upper())
	out = OrderedDict()
	if values is None:
		return out
	available = table['value_columns']
	for name in _select_columns(table, columns):
		index = available.index(name)
		out[name] = values[index] if index < len(values) else None
	return out


# ---------------------------------------------------------------------------
# Reduction
# ---------------------------------------------------------------------------

def reduce_values(values, how):
	"""Reduce a list of numbers to one.

	``'absmax'`` / ``'absmin'`` pick by magnitude but return the value with
	its **sign intact** — a Riks analysis that snaps back drives the response
	negative, and the peak of that curve is a negative number.
	"""
	clean = [v for v in values if v is not None]
	if how == 'count':
		return float(len(clean))
	if not clean:
		raise ValueError("no values to reduce with '{0}'".format(how))
	if how == 'last':
		return float(clean[-1])
	if how == 'first':
		return float(clean[0])
	if how == 'max':
		return float(max(clean))
	if how == 'min':
		return float(min(clean))
	if how == 'sum':
		return float(sum(clean))
	if how == 'mean':
		return float(sum(clean)) / len(clean)
	if how == 'range':
		return float(max(clean) - min(clean))
	if how == 'absmax':
		return float(max(clean, key=abs))
	if how == 'absmin':
		return float(min(clean, key=abs))
	raise ValueError("unknown reduce: {0!r}".format(how))


def reduce_rows(rows, header, column, how):
	"""Reduce one named column of a ``(header, rows)`` pair to a scalar."""
	upper = [str(h).upper() for h in header]
	name = str(column).upper()
	if name not in upper:
		raise ValueError("column {0!r} not in {1!r}".format(column, header))
	index = upper.index(name)
	return reduce_values([row[index] for row in rows], how)
