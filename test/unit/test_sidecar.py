"""Tests for the sidecar/field contract in ABQflow.helpers.convert (IMP-06, SC-01).

Covers is_sidecar / resolve_sidecar / load_field / iter_fields / degenerate_from_array's
sidecar guard, and the row-order contract between iter_fields and degenerate_from_array.

Run: pytest test/unit/test_sidecar.py -v
"""

import os
import tempfile

import numpy as np
import pytest

from ABQflow import (
	JobOutcome,
	degenerate_from_array,
	is_sidecar,
	iter_fields,
	load_field,
	resolve_sidecar,
)


# ============================================================
# Sidecar contract (IMP-06)
# ============================================================

def test_is_sidecar():
	assert is_sidecar({'__file__': 'test.csv', 'format': 'csv'})
	assert not is_sidecar({'normal': 'dict'})
	assert not is_sidecar([1, 2, 3])
	assert not is_sidecar(None)
	assert not is_sidecar("string")


def test_resolve_sidecar():
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'data.csv')
		with open(csv_path, 'w') as f:
			f.write("col1,col2\n1.0,2.0\n3.0,4.0\n")

		envelope = {'__file__': 'data.csv', 'format': 'csv', 'shape': [2, 2]}

		abspath, meta = resolve_sidecar(envelope, d, load=False)
		assert abspath == csv_path
		assert meta == {'format': 'csv', 'shape': [2, 2]}

		data, meta = resolve_sidecar(envelope, d, load=True)
		assert data.shape == (2, 2)
		assert data[0, 0] == 1.0


def test_degenerate_sidecar_guard():
	oc = JobOutcome('test', 'COMPLETED', results={'big': {'__file__': 'f.csv'}})
	with pytest.raises(ValueError, match='sidecar field'):
		degenerate_from_array([oc], ['big'])


# ============================================================
# SC-01: sidecar field loading (load_field / iter_fields)
# ============================================================

@pytest.fixture
def csv_outcome():
	"""Create a JobOutcome with both inline and sidecar results."""
	with tempfile.TemporaryDirectory() as d:
		# Write a sidecar CSV
		csv_path = os.path.join(d, 'job001_stress.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("x,y,z\n1.0,2.0,3.0\n4.0,5.0,6.0\n7.0,8.0,9.0\n")

		oc = JobOutcome(
			job_name='job001',
			status='COMPLETED',
			results={
				'mass': 0.42,
				'stress': {
					'__file__': 'job001_stress.csv',
					'format': 'csv',
					'shape': [3, 3],
					'columns': ['x', 'y', 'z'],
				},
			},
			output_dir=d,
		)
		yield oc


# -- T1: dual-representation normalisation -----------------------------------

def test_load_field_inline(csv_outcome):
	"""Inline scalar is returned as ndarray."""
	arr = load_field(csv_outcome, 'mass')
	assert isinstance(arr, np.ndarray)
	assert arr.shape == ()
	assert arr.item() == 0.42


def test_load_field_sidecar(csv_outcome):
	"""Sidecar envelope loads CSV into ndarray."""
	arr = load_field(csv_outcome, 'stress')
	assert isinstance(arr, np.ndarray)
	assert arr.shape == (3, 3)
	assert arr[0, 0] == 1.0
	assert arr[2, 2] == 9.0


def test_load_field_missing_result(csv_outcome):
	"""Missing result_name returns None."""
	assert load_field(csv_outcome, 'nonexistent') is None


def test_load_field_none_results():
	"""Outcome with results=None returns None."""
	oc = JobOutcome('j', 'COMPLETED', results=None)
	assert load_field(oc, 'anything') is None


def test_load_field_no_output_dir():
	"""Outcome without output_dir returns None + warning."""
	oc = JobOutcome('j', 'COMPLETED', results={'f': {'__file__': 'x.csv'}})
	with pytest.warns(UserWarning, match='no output_dir'):
		assert load_field(oc, 'f') is None


# -- T3: iter_fields on_missing modes ----------------------------------------

def _make_ocs(d):
	"""Helper: create outcomes from {name: results_or_None}."""
	ocs = []
	for name, results in [('job_2', {'v': [1.0, 2.0]}),
						  ('job_1', {'v': [3.0, 4.0]}),
						  ('job_10', {'v': None})]:
		ocs.append(JobOutcome(name, 'COMPLETED', results=results))
	return ocs


def test_iter_fields_natural_sort():
	"""iter_fields yields in natural key order (job_1 before job_2 before job_10)."""
	ocs = _make_ocs(None)
	# Use on_missing='none' so job_10 (v=None) is included
	names = [n for n, _ in iter_fields(ocs, 'v', on_missing='none')]
	assert names == ['job_1', 'job_2', 'job_10']


def test_iter_fields_on_missing_skip():
	"""on_missing='skip' omits missing jobs + issues summary warning."""
	ocs = _make_ocs(None)
	with pytest.warns(UserWarning, match='skipped due to missing'):
		results = list(iter_fields(ocs, 'v', on_missing='skip'))
	assert len(results) == 2
	assert results[0][0] == 'job_1'
	assert np.array_equal(results[0][1], [3.0, 4.0])


def test_iter_fields_on_missing_none():
	"""on_missing='none' yields (name, None) for missing jobs."""
	ocs = _make_ocs(None)
	results = list(iter_fields(ocs, 'v', on_missing='none'))
	assert len(results) == 3
	assert results[2] == ('job_10', None)


def test_iter_fields_on_missing_raise():
	"""on_missing='raise' raises on first missing field."""
	ocs = _make_ocs(None)
	with pytest.raises(ValueError, match="missing for job 'job_10'"):
		list(iter_fields(ocs, 'v', on_missing='raise'))


def test_iter_fields_bad_on_missing():
	"""Invalid on_missing value raises ValueError."""
	with pytest.raises(ValueError, match="on_missing must be"):
		list(iter_fields([], 'x', on_missing='bad'))


# -- T4: migration scenario ---------------------------------------------------

def test_load_field_missing_csv():
	"""File referenced by sidecar no longer exists → None + warning."""
	with tempfile.TemporaryDirectory() as d:
		oc = JobOutcome('j', 'COMPLETED',
			results={'f': {'__file__': 'gone.csv', 'format': 'csv'}},
			output_dir=d)
		with pytest.warns(UserWarning, match='missing or empty'):
			assert load_field(oc, 'f') is None


# -- T5: numeric_only + shape mismatch ---------------------------------------

def test_load_field_numeric_only_drops_string_col():
	"""Non-numeric column is dropped with warning when numeric_only=True."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'mixed.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("label,val\nabc,1.0\ndef,2.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'mixed': {'__file__': 'mixed.csv', 'format': 'csv'}},
			output_dir=d)
		with pytest.warns(UserWarning, match="Non-numeric column"):
			arr = load_field(oc, 'mixed')
		assert arr.shape == (2, 1)  # only 'val' column survives
		assert arr[0, 0] == 1.0


def test_load_field_numeric_only_false():
	"""numeric_only=False preserves string columns as object array."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'mixed.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("label,val\nabc,1.0\ndef,2.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'mixed': {'__file__': 'mixed.csv', 'format': 'csv'}},
			output_dir=d)
		arr = load_field(oc, 'mixed', numeric_only=False)
		assert arr.shape == (2, 2)
		assert arr[0, 0] == 'abc'
		assert arr[0, 1] == '1.0'


def test_load_field_shape_mismatch_warns():
	"""Claimed shape differs from file → warning, data still loaded."""
	with tempfile.TemporaryDirectory() as d:
		csv_path = os.path.join(d, 'data.csv')
		with open(csv_path, 'w', newline='') as f:
			f.write("a,b\n1.0,2.0\n3.0,4.0\n")

		oc = JobOutcome('j', 'COMPLETED',
			results={'f': {'__file__': 'data.csv', 'format': 'csv',
						   'shape': [999, 2]}},
			output_dir=d)
		with pytest.warns(UserWarning, match='shape mismatch'):
			arr = load_field(oc, 'f')
		assert arr.shape == (2, 2)


# -- T2: row-order contract ---------------------------------------------------

def test_iter_fields_aligns_with_degenerate():
	"""iter_fields('none') row i corresponds to degenerate row i (same job)."""
	with tempfile.TemporaryDirectory() as d:
		ocs = [
			JobOutcome('job_2', 'COMPLETED',
				results={'mass': 2.0},
				output_dir=d),
			JobOutcome('job_1', 'COMPLETED',
				results={'mass': 1.0},
				output_dir=d),
			JobOutcome('job_3', 'COMPLETED',
				results={'mass': 3.0},
				output_dir=d),
		]
		# degenerate sorts by natural key (job_1, job_2, job_3)
		mat = degenerate_from_array(ocs, ['mass'])
		assert mat[0, 0] == 1.0
		assert mat[1, 0] == 2.0
		assert mat[2, 0] == 3.0

		# iter_fields('none') uses same sort → same order
		fields = list(iter_fields(ocs, 'mass', on_missing='none'))
		assert fields[0][0] == 'job_1'
		assert fields[1][0] == 'job_2'
		assert fields[2][0] == 'job_3'
