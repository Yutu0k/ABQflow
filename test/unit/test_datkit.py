"""datkit — parsing Abaqus ``.dat`` printed output.

The fixtures under ``test/fixtures/dat/`` are hand-written miniatures of real
files, byte-exact down to the whitespace: the separator lines alternate between
truly empty and exactly two spaces, the ``FOOT-``/``NOTE`` heading is split
across two lines, and the echo section carries the two traps that a naive
parser falls into — an ``E L E M E N T   Q U A L I T Y  C H E C K S`` heading
that is not an element output table, and echoed comment text containing the
words ``INCREMENT``, ``SUMMARY`` and ``N O D E   O U T P U T``.

Run: pytest test/unit/test_datkit.py -v
"""

from __future__ import annotations

import io
import os

import pytest

from ABQflow import datkit

FIXTURES = os.path.join(
	os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
	'fixtures', 'dat',
)

RIKS = os.path.join(FIXTURES, 'riks_complete.dat')
RIKS_BAD = os.path.join(FIXTURES, 'riks_incomplete.dat')
ELEMENT = os.path.join(FIXTURES, 'element_output.dat')
MULTISTEP = os.path.join(FIXTURES, 'static_multistep.dat')
# Trimmed from genuine Abaqus 2026 output for
# ``*EL PRINT, ELSET=ALL, POSITION=INTEGRATION POINTS, SUMMARY=YES`` over a
# mixed CPS3/CPS4R mesh.  Structural lines are byte-for-byte as Abaqus wrote
# them; only the bulk data rows were cut.
EL_PRINT = os.path.join(FIXTURES, 'el_print_mixed.dat')

NSET = 'JOB001_INSPECTION_POSITION_NODE1536604'


@pytest.fixture(scope='module')
def riks():
	return datkit.parse(RIKS)


# ======================== structure ========================

def test_fixtures_exist():
	for path in (RIKS, RIKS_BAD, ELEMENT, MULTISTEP, EL_PRINT):
		assert os.path.isfile(path), f"missing fixture {path}"


def test_step_and_increment_counts(riks):
	assert [s['step'] for s in riks['steps']] == [1]
	assert riks['steps'][0]['title'] == 'STATIC ANALYSIS'
	assert riks['increment_count'] == 2
	assert [i['increment'] for i in riks['increments']] == [1, 2]
	assert riks['table_count'] == 2
	assert riks['warnings'] == []


def test_echo_section_yields_no_tables(riks):
	"""The echo traps must not be mistaken for output.

	``E L E M E N T   Q U A L I T Y  C H E C K S`` despaces to ``ELEMENT
	QUALITY CHECKS`` — two spaces before ``C H E C K S``, not three — so it
	can never collide with ``ELEMENT OUTPUT``.  The echoed comments naming
	``INCREMENT``/``SUMMARY`` must not end the echo state either.
	"""
	kinds = [t['kind'] for inc in riks['increments'] for t in inc['tables']]
	assert kinds == ['node', 'node']

	with io.open(RIKS, 'r', encoding='utf-8') as f:
		text = f.read()
	assert 'E L E M E N T   Q U A L I T Y  C H E C K S' in text
	assert 'says INCREMENT 9 SUMMARY' in text


def test_header_footnote_columns_are_merged(riks):
	table = riks['increments'][0]['tables'][0]
	assert table['columns'] == [
		'NODE', 'FOOTNOTE', 'U1', 'U2', 'U3', 'UR1', 'UR2', 'UR3',
		'COOR1', 'COOR2', 'COOR3',
	]
	assert table['label_columns'] == ['NODE']
	assert table['footnote_index'] == 1
	assert table['value_columns'][0] == 'U1'
	assert len(table['value_columns']) == 9


def test_data_row_values(riks):
	row = riks['increments'][0]['tables'][0]['rows'][0]
	assert row[0] == 1536604
	assert row[1] == ''                       # empty footnote column
	assert row[2] == pytest.approx(4.2961278e-03)
	assert row[3] == pytest.approx(1.7114364e-01)
	assert row[-1] == pytest.approx(5.6296051e+02)


def test_summary_rows_use_their_own_grids(riks):
	"""MAXIMUM/MINIMUM are G12.4 on a 12-wide grid; AT NODE's is shifted one
	column left.  Splitting on whitespace is what makes both work."""
	table = riks['increments'][0]['tables'][0]
	assert set(table['summary']) == {'MAXIMUM', 'MAXIMUM AT', 'MINIMUM', 'MINIMUM AT'}
	assert table['summary']['MAXIMUM'][0] == pytest.approx(4.2961e-03)
	assert table['summary']['MAXIMUM'][-1] == pytest.approx(563.0)
	assert table['summary']['MAXIMUM AT'] == [1536604] * 9
	assert all(isinstance(v, int) for v in table['summary']['MAXIMUM AT'])


def test_set_name_matching_is_case_insensitive_and_globbable(riks):
	assert datkit.match_set(NSET, NSET.lower())
	assert datkit.match_set(NSET, '*inspection_position*')
	assert not datkit.match_set(NSET, 'OTHER_SET')
	assert not datkit.match_set(None, 'ANY')
	assert datkit.match_set(None, None)

	assert len(datkit.select_tables(riks, kind='node', set_name='*INSPECTION*',
									increment='all')) == 2
	assert datkit.select_tables(riks, set_name='NOPE', increment='all') == []


# ======================== selectors ========================

@pytest.mark.parametrize('selector, expected', [
	('all', [1, 2]),
	('first', [1]),
	('last', [2]),
	(1, [1]),
	(2, [2]),
	(-1, [2]),
	(-2, [1]),
	(-3, []),
	([1, 2], [1, 2]),
])
def test_select_increments(riks, selector, expected):
	picked = datkit.select_increments(riks, increment=selector)
	assert [i['increment'] for i in picked] == expected


def test_streaming_selector_drops_what_it_does_not_keep():
	"""``increments='last'`` retains one increment, but still reports how many
	the file held — that is the whole point of streaming a 6 MB file."""
	doc = datkit.parse(RIKS, increments='last')
	assert len(doc['increments']) == 1
	assert doc['increments'][0]['increment'] == 2
	assert doc['increment_count'] == 2

	first = datkit.parse(RIKS, increments='first')
	assert [i['increment'] for i in first['increments']] == [1]
	assert first['increment_count'] == 2


def test_keep_rows_false_retains_shape_only():
	doc = datkit.parse(RIKS, keep_rows=False)
	table = doc['increments'][0]['tables'][0]
	assert table['rows'] == []
	assert table['shape'] == [1, 11]
	assert table['summary']['MAXIMUM AT'] == [1536604] * 9


def test_kind_and_set_filters_drop_rows_at_the_source():
	doc = datkit.parse(RIKS, kinds='element')
	assert doc['table_count'] == 0
	assert all(not inc['tables'] for inc in doc['increments'])


# ======================== element output ========================

def test_element_table_labels_and_page_break_continuation():
	"""A table split across a page break re-prints its banner and header; the
	parser must merge the halves rather than emit two tables."""
	doc = datkit.parse(ELEMENT)
	tables = datkit.select_tables(doc, kind='element', increment='last')
	assert len(tables) == 1

	table = tables[0]
	assert table['label_columns'] == ['ELEMENT', 'PT']
	assert table['footnote_index'] == 2
	assert table['value_columns'] == ['S11', 'S22', 'S12', 'MISES']
	assert table['set'] is None               # no "THE FOLLOWING TABLE" line
	assert [(r[0], r[1]) for r in table['rows']] == [(1, 1), (1, 2), (2, 1), (2, 2)]
	assert table['rows'][-1][-1] == pytest.approx(301.16441)
	assert table['summary']['TOTAL'][0] == pytest.approx(460.0)


def test_at_element_rows_are_keyed_by_the_row_they_annotate():
	"""Abaqus prints ``AT ELEMENT`` under MAXIMUM and again under MINIMUM.  The
	single-node reference dataset hides the difference; here they differ, and
	keying both as ``AT ELEMENT`` would silently lose the first."""
	doc = datkit.parse(ELEMENT)
	table = datkit.select_tables(doc, kind='element', increment='last')[0]
	assert table['summary']['MAXIMUM AT'] == [2, 2, 1, 2]
	assert table['summary']['MINIMUM AT'] == [1, 1, 2, 1]

	where = datkit.summary(table, 'MAXIMUM AT', ['MISES'])
	assert where['MISES'] == 2


# ======================== real *EL PRINT output ========================
# These four cases come from genuine Abaqus 2026 output; each one is something
# the hand-written fixtures above got wrong until a real file was parsed.

@pytest.fixture(scope='module')
def el_print():
	return datkit.parse(EL_PRINT)


def test_el_print_one_table_per_element_type(el_print):
	"""``*EL PRINT`` over a mixed mesh prints one table per element type but
	only ONE ``E L E M E N T   O U T P U T`` banner — every table after the
	first begins at its description line.  Appending the second type's rows to
	the first table would also let its summary overwrite the first's."""
	tables = datkit.select_tables(el_print, kind='element', increment='last')
	assert len(tables) == 2

	assert 'ELEMENT TYPE CPS3 ' in tables[0]['description']
	assert 'ELEMENT TYPE CPS4R ' in tables[1]['description']
	assert tables[0]['rows'][0][0] == 1        # CPS3 elements
	assert tables[1]['rows'][0][0] == 51       # CPS4R elements start later
	assert tables[0]['summary']['MAXIMUM'] == [pytest.approx(3058.0)]
	assert tables[1]['summary']['MAXIMUM'] == [pytest.approx(4525.0)]


def test_el_print_set_name_survives_a_wrapped_description(el_print):
	"""Abaqus wraps the description at the page width, so the set name lands on
	a line of its own -- reading only the first line loses it entirely."""
	for table in datkit.select_tables(el_print, kind='element', increment='last'):
		assert table['set'] == 'ASSEMBLY_ALL'
	assert datkit.select_tables(el_print, kind='element',
								set_name='assembly_*', increment='last')


def test_el_print_bare_element_locator_rows(el_print):
	"""Element output labels its locator row a bare ``ELEMENT``, not ``AT
	ELEMENT`` as node output's ``AT NODE`` would suggest."""
	table = datkit.select_tables(el_print, kind='element', increment='last')[-1]
	assert table['summary']['MAXIMUM AT'] == [200]
	assert table['summary']['MINIMUM AT'] == [175]
	assert table['summary']['MINIMUM'] == [pytest.approx(563.2)]


def test_el_print_columns_and_reduction(el_print):
	"""End to end: peak MISES over both element tables, as a task would ask."""
	tables = datkit.select_tables(el_print, kind='element', increment='last')
	table = tables[0]
	assert table['columns'] == ['ELEMENT', 'PT', 'FOOTNOTE', 'MISES']
	assert table['label_columns'] == ['ELEMENT', 'PT']
	assert table['value_columns'] == ['MISES']

	header, rows = datkit.to_rows(tables, columns=['MISES'])
	assert header == ['ELEMENT', 'PT', 'MISES']
	assert len(rows) == 13                      # 5 CPS3 + 8 CPS4R in the fixture
	assert datkit.reduce_rows(rows, header, 'MISES', 'max') == pytest.approx(4473.0)
	# The whole-model peak lives in the summary rows: the fixture keeps only the
	# first few data rows of each table, so reducing over them understates it.
	peaks = [datkit.summary(t, 'MAXIMUM')['MISES'] for t in tables]
	assert max(peaks) == pytest.approx(4525.0)


def test_el_print_step_time_metadata(el_print):
	inc = el_print['increments'][0]
	assert inc['meta']['step_time_completed'] == pytest.approx(1.0)
	assert inc['meta']['total_time_completed'] == pytest.approx(1.0)
	assert el_print['completed'] is True


# ======================== termination ========================

def test_completed_run(riks):
	assert riks['completed'] is True
	assert riks['terminator'] == 'completed'


def test_increment_limit_run_is_not_completed():
	"""``ANALYSIS COMPLETE`` is printed either way, so only ``THE ANALYSIS HAS
	BEEN COMPLETED`` counts as success."""
	doc = datkit.parse(RIKS_BAD)
	assert doc['completed'] is False
	assert doc['terminator'] == 'inc_limit'

	with io.open(RIKS_BAD, 'r', encoding='utf-8') as f:
		assert 'ANALYSIS COMPLETE' in f.read()


def test_riks_snapback_gives_a_negative_lpf():
	doc = datkit.parse(RIKS_BAD)
	factors = [i['meta']['load_proportionality_factor'] for i in doc['increments']]
	assert factors == [pytest.approx(0.1028), pytest.approx(-1.111)]


@pytest.mark.parametrize('path, expected', [
	(RIKS, {'completed': True, 'terminator': 'completed'}),
	(RIKS_BAD, {'completed': False, 'terminator': 'inc_limit'}),
])
def test_scan_status_agrees_with_a_full_parse(path, expected):
	assert datkit.scan_status(path) == expected
	doc = datkit.parse(path)
	assert doc['completed'] == expected['completed']
	assert doc['terminator'] == expected['terminator']


# ======================== multi-step ========================

def test_multistep_steps_increments_and_time_metadata():
	doc = datkit.parse(MULTISTEP)
	assert [s['step'] for s in doc['steps']] == [1, 2]

	step2 = datkit.select_increments(doc, step=2, increment='all')
	assert [i['increment'] for i in step2] == [1, 2]
	assert step2[0]['meta']['step_time_completed'] == pytest.approx(0.5)
	assert step2[1]['meta']['total_time_completed'] == pytest.approx(2.0)

	assert [i['increment'] for i in
			datkit.select_increments(doc, step='last', increment='all')] == [1, 2]


def test_output_without_an_increment_summary_is_still_attributed():
	"""Step 1 prints its table with no ``INCREMENT n SUMMARY`` — the page
	banner is the only context, and the table must not be lost."""
	doc = datkit.parse(MULTISTEP)
	tables = datkit.select_tables(doc, kind='node', step=1, increment='all')
	assert len(tables) == 1
	assert tables[0]['rows'][0][2] == pytest.approx(0.1)


def test_unrecognisable_layout_degrades_to_raw_instead_of_raising():
	doc = datkit.parse(MULTISTEP)
	raw = [t for inc in doc['increments'] for t in inc['tables'] if t['kind'] == 'raw']
	assert len(raw) == 1
	assert raw[0]['rows'] == []
	assert any('ALLSE' in line for line in raw[0]['lines'])
	assert not raw[0]['lines'][-1].strip() or raw[0]['lines'][-1].strip()


# ======================== shaping ========================

def test_to_rows_header_selection_and_index_columns(riks):
	tables = datkit.select_tables(riks, kind='node', increment='all')
	header, rows = datkit.to_rows(
		tables, columns=['U2'],
		index_columns=['step', 'increment', 'load_proportionality_factor'],
		include_labels=False)

	assert header == ['step', 'increment', 'load_proportionality_factor', 'U2']
	assert len(rows) == 2
	assert rows[0][:3] == [1, 1, pytest.approx(0.1004)]
	assert rows[1][3] == pytest.approx(1.7844708)


def test_to_rows_keeps_labels_and_drops_the_footnote_by_default(riks):
	"""``load_field`` coerces every cell to a float, so the text footnote
	column must stay out unless it is asked for by name."""
	tables = datkit.select_tables(riks, kind='node', increment='last')
	header, rows = datkit.to_rows(tables, columns=['U1', 'U2'])
	assert header == ['NODE', 'U1', 'U2']
	assert all(isinstance(cell, (int, float)) for row in rows for cell in row)

	header, rows = datkit.to_rows(tables, columns=['U1'], include_footnote=True)
	assert header == ['NODE', 'FOOTNOTE', 'U1']
	assert rows[0][1] == ''


def test_to_rows_label_filter(riks):
	tables = datkit.select_tables(riks, kind='node', increment='all')
	_header, rows = datkit.to_rows(tables, labels=[1536604])
	assert len(rows) == 2
	_header, rows = datkit.to_rows(tables, labels=[999])
	assert rows == []


def test_to_rows_on_nothing_is_empty():
	assert datkit.to_rows([]) == ([], [])


def test_column_values_and_summary(riks):
	table = datkit.select_tables(riks, kind='node', increment='last')[0]
	assert datkit.column_values(table, 'U2') == [pytest.approx(1.7844708)]

	picked = datkit.summary(table, 'MAXIMUM', ['U2', 'U1'])
	assert list(picked) == ['U2', 'U1']            # selection order preserved
	assert picked['U2'] == pytest.approx(1.784)
	assert datkit.summary(table, 'NO SUCH ROW') == {}


# ======================== reduction ========================

@pytest.mark.parametrize('how, expected', [
	('last', -9.7767031),
	('first', 0.17114364),
	('max', 0.17114364),
	('min', -9.7767031),
	('absmax', -9.7767031),     # magnitude picks it, the sign survives
	('absmin', 0.17114364),
	('count', 2.0),
	('sum', 0.17114364 - 9.7767031),
	('mean', (0.17114364 - 9.7767031) / 2),
	('range', 0.17114364 + 9.7767031),
])
def test_reduce_rows(how, expected):
	doc = datkit.parse(RIKS_BAD)
	tables = datkit.select_tables(doc, kind='node', increment='all')
	header, rows = datkit.to_rows(tables, columns=['U2'], include_labels=False)
	assert datkit.reduce_rows(rows, header, 'U2', how) == pytest.approx(expected)


def test_reduce_rejects_unknown_column_and_verb():
	with pytest.raises(ValueError, match='not in'):
		datkit.reduce_rows([[1.0]], ['U2'], 'U9', 'max')
	with pytest.raises(ValueError, match='unknown reduce'):
		datkit.reduce_values([1.0], 'median')
	with pytest.raises(ValueError, match='no values'):
		datkit.reduce_values([None], 'max')


# ======================== value parsing ========================

@pytest.mark.parametrize('token, expected', [
	('1.0E-03', 1.0e-3),
	('1.0D-03', 1.0e-3),
	('-4.0754050E-04', -4.075405e-4),
	('4.2961278-303', 4.2961278e-303),   # Fortran drops 'E' for 3-digit exponents
	('  0.1711  ', 0.1711),
	('1536604', 1536604.0),
	('***********', None),
	('NaN', None),
	('Infinity', None),
	('', None),
	(None, None),
	('not-a-number', None),
])
def test_to_float(token, expected):
	result = datkit.to_float(token)
	if expected is None:
		assert result is None
	else:
		assert result == pytest.approx(expected)


# ======================== I/O behaviour ========================

def test_crlf_copy_parses_identically(tmp_path):
	"""Abaqus writes LF only, but a file that travelled through a Windows tool
	may arrive with CRLF; the result must not change."""
	with io.open(RIKS, 'r', encoding='utf-8') as f:
		text = f.read()
	target = tmp_path / 'crlf.dat'
	with io.open(str(target), 'w', encoding='utf-8', newline='') as f:
		f.write(text.replace('\n', '\r\n'))

	converted = datkit.parse(str(target))
	original = datkit.parse(RIKS)
	assert converted['increment_count'] == original['increment_count']
	assert converted['completed'] == original['completed']
	assert (converted['increments'][-1]['tables'][0]['rows']
			== original['increments'][-1]['tables'][0]['rows'])


def test_iter_tables_matches_parse():
	streamed = list(datkit.iter_tables(RIKS, kinds='node'))
	doc = datkit.parse(RIKS)
	batched = [t for inc in doc['increments'] for t in inc['tables']]
	assert len(streamed) == len(batched) == 2
	assert [t['rows'] for t in streamed] == [t['rows'] for t in batched]
	assert all('_keep' not in t for t in streamed)


def test_missing_file_names_the_path(tmp_path):
	missing = str(tmp_path / 'nope.dat')
	with pytest.raises(IOError) as excinfo:
		datkit.parse(missing)
	assert 'nope.dat' in str(excinfo.value)
	with pytest.raises(IOError):
		datkit.scan_status(missing)
