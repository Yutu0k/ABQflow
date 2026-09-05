"""Tests for the spec generators' contract with base_spec.

A generator fills some fields in for you.  Two things must hold: a field it
supplies may be left unset (you should not have to invent a value that gets
thrown away), and a value you set there anyway must not vanish in silence.

Run: pytest test/unit/test_convert_generators.py -v
"""

import logging
import warnings

import numpy as np
import pytest

from ABQflow import (
	BatchAbaqusProcessor,
	JobSpec,
	PreparationSpec,
	extract_json,
	generate_from_array,
	generate_from_inp_files,
)
from ABQflow.core.strategies import InpModifyStrategy
from ABQflow.helpers.constant import RESULT_BEGIN, RESULT_END


@pytest.fixture
def inp_dir(tmp_path):
	"""Two finished decks for generate_from_inp_files to walk."""
	for name in ('case_a.inp', 'case_b.inp'):
		(tmp_path / name).write_text('*Step\n*Static\n*End Step\n', encoding='utf-8')
	return tmp_path


def _base(**prep_kwargs):
	return JobSpec('base', preparation=PreparationSpec(**prep_kwargs))


def _warnings_from(fn):
	with warnings.catch_warnings(record=True) as caught:
		warnings.simplefilter('always')
		result = fn()
	return result, [str(w.message) for w in caught]


# ============================================================
# The fields a generator supplies may be left unset
# ============================================================

def test_preparation_spec_needs_no_source_path():
	"""The point of the change: a base spec headed for generate_from_inp_files
	should not have to name an unrelated INP just to construct."""
	spec = PreparationSpec(kind='existing_inp')
	assert spec.source_path == ''


def test_a_clean_base_spec_generates_without_warnings(inp_dir):
	base = _base(kind='existing_inp')
	specs, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert msgs == []
	assert [s.preparation.kind for s in specs] == ['existing_inp'] * 2
	assert all(s.preparation.source_path.endswith('.inp') for s in specs)


def test_generate_from_array_needs_no_params():
	base = _base(kind='inp_based', source_path='tpl.inp')
	specs, msgs = _warnings_from(
		lambda: generate_from_array(np.array([[1.0], [2.0]]), ['E'], base))

	assert msgs == []
	assert [s.preparation.params for s in specs] == [{'E': 1.0}, {'E': 2.0}]


# ============================================================
# A value the generator will discard is reported
# ============================================================

def test_a_discarded_source_path_is_reported(inp_dir):
	base = _base(kind='existing_inp', source_path='./some/unrelated.inp')
	_, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert len(msgs) == 1
	assert 'source_path' in msgs[0]
	assert 'unrelated.inp' in msgs[0]


def test_a_discarded_kind_is_reported(inp_dir):
	base = _base(kind='inp_based')
	_, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert len(msgs) == 1
	assert 'kind' in msgs[0] and 'inp_based' in msgs[0]


def test_discarded_params_are_reported(inp_dir):
	base = _base(kind='existing_inp', params={'E': 1})
	_, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert len(msgs) == 1
	assert 'params' in msgs[0]


def test_several_discarded_settings_are_reported_together(inp_dir):
	base = _base(kind='inp_based', source_path='x.inp', params={'E': 1})
	_, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert len(msgs) == 1
	for field in ('kind', 'source_path', 'params'):
		assert field in msgs[0]


def test_the_warning_fires_once_not_once_per_file(inp_dir):
	"""The same mistake repeated N times is still one mistake — the old
	per-file warning buried the notebook output under duplicates."""
	for extra in ('case_c.inp', 'case_d.inp', 'case_e.inp'):
		(inp_dir / extra).write_text('*Step\n*End Step\n', encoding='utf-8')
	base = _base(kind='inp_based')

	specs, msgs = _warnings_from(
		lambda: generate_from_inp_files(str(inp_dir / '*.inp'), base))

	assert len(specs) == 5
	assert len(msgs) == 1


def test_generate_from_array_reports_discarded_params():
	base = _base(kind='inp_based', source_path='tpl.inp', params={'E': 999})
	_, msgs = _warnings_from(
		lambda: generate_from_array(np.array([[1.0]]), ['E'], base))

	assert len(msgs) == 1
	assert 'params' in msgs[0]


def test_generate_from_array_on_existing_inp_is_called_out():
	"""Not a discarded value but a dead sweep: existing_inp substitutes nothing,
	so every job would run the identical deck."""
	base = _base(kind='existing_inp', source_path='finished.inp')
	_, msgs = _warnings_from(
		lambda: generate_from_array(np.array([[1.0], [2.0]]), ['E'], base))

	assert any('no effect' in m for m in msgs)


# ============================================================
# meta belongs to the caller
# ============================================================

def test_generate_from_inp_files_merges_meta_rather_than_replacing_it(inp_dir):
	base = JobSpec('base', preparation=PreparationSpec(kind='existing_inp'),
				meta={'campaign': 'A'})

	specs = generate_from_inp_files(str(inp_dir / '*.inp'), base)

	for s in specs:
		assert s.meta['campaign'] == 'A'
		assert s.meta['source_inp'].endswith('.inp')


# ============================================================
# What happens when nothing supplies the missing field
# ============================================================

def test_an_empty_source_path_reaching_preparation_names_both_exits(tmp_path, caplog):
	log = logging.getLogger('empty_source')
	ctx_dir = tmp_path / 'job'
	ctx_dir.mkdir()
	from ABQflow.core.context import JobContext
	ctx = JobContext(job_name='job', output_dir=str(ctx_dir), cpus=1)

	with caplog.at_level(logging.ERROR, logger='empty_source'):
		ok = InpModifyStrategy('', {}).prepare(ctx, None, log)

	assert not ok
	assert 'source_path is empty' in caplog.text
	assert 'generate_from_inp_files' in caplog.text


def test_batch_data_must_hold_jobspecs(tmp_path):
	"""Dict configs used to be upgraded silently; now the type is the contract."""
	with pytest.raises(TypeError, match='must contain JobSpec'):
		BatchAbaqusProcessor(
			[{'job_name': 'j', 'type': 'inp_based', 'base_inp_path': 'x.inp'}],
			str(tmp_path), cpus_per_job=1)


# ============================================================
# Hook output must be marked, not guessed at
# ============================================================

def test_extract_json_reads_a_marked_payload():
	text = f'Abaqus banner {{noise}}\n{RESULT_BEGIN}\n{{"x": 1}}\n{RESULT_END}\ntrailer\n'
	assert extract_json(text) == {'x': 1}


def test_extract_json_refuses_to_guess_at_an_unmarked_payload():
	"""The removed brace-scan picked whichever '{' came last, which on a real
	Abaqus run is as likely to be banner noise as the hook's result."""
	with pytest.raises(ValueError, match='markers'):
		extract_json('some output {"x": 1} and then {not json at all')
