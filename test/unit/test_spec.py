"""Tests for ABQflow.core.spec — JobSpec / PreparationSpec / SubroutineSpec validation.

Run: pytest test/unit/test_spec.py -v
"""

import pytest

from ABQflow import JobSpec, PreparationSpec, SubroutineSpec


# ============================================================
# Preflight (IMP-04)
# ============================================================

def test_jobspec_preflight_validation():
	for v in ('syntaxcheck', 'datacheck', None):
		spec = JobSpec('j', workflow='monolithic', monolithic_script='t.py', preflight=v)
		assert spec.preflight == v

	with pytest.raises(ValueError, match='preflight'):
		JobSpec('j', workflow='monolithic', monolithic_script='t.py', preflight='bad')


# ============================================================
# Subroutine support
# ============================================================

def test_subroutine_spec_defaults_and_validation():
	s = SubroutineSpec('umat.for')
	assert s.language == 'fortran' and s.solver == 'standard' and s.precompiled is False

	with pytest.raises(ValueError, match='language'):
		SubroutineSpec('umat.for', language='pascal')

	with pytest.raises(ValueError, match='solver'):
		SubroutineSpec('umat.for', solver='quantum')


def test_jobspec_with_subroutine():
	spec = JobSpec('j', preparation=PreparationSpec(kind='existing_inp', source_path='d.inp'),
					subroutine=SubroutineSpec('vumat.for', solver='explicit'))
	assert spec.subroutine.solver == 'explicit'
