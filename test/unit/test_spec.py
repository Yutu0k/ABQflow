"""Tests for ABQflow.core.spec — JobSpec / PreparationSpec / SubroutineSpec validation.

Run: pytest test/unit/test_spec.py -v
"""

import pytest

from ABQflow import HOOK_SOURCES, HookSpec, JobSpec, PreparationSpec, SubroutineSpec


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


# ============================================================
# HookSpec.source — which artifact a post-extraction hook reads
# ============================================================

def test_hookspec_source_defaults_to_odb():
	"""Every spec written before `source` existed must keep working verbatim."""
	hook = HookSpec('get_stress.py', tasks=[{'result_name': 'sigma'}])
	assert hook.source == 'odb'
	assert HOOK_SOURCES == ('odb', 'dat')


@pytest.mark.parametrize('source', HOOK_SOURCES)
def test_hookspec_accepts_every_registered_source(source):
	assert HookSpec('h.py', source=source).source == source


def test_hookspec_rejects_an_unknown_source():
	with pytest.raises(ValueError, match=r"source must be one of \('odb', 'dat'\)"):
		HookSpec('h.py', source='fil')


def test_jobspec_from_dict_carries_the_source_through():
	spec = JobSpec.from_dict({
		'job_name': 'j',
		'base_inp_path': 'base.inp',
		'post_extraction': [
			{'script_path': 'odb_hook.py', 'tasks': [{'result_name': 'a'}]},
			{'script_path': 'dat_hook.py', 'source': 'dat',
				'tasks': [{'result_name': 'b'}]},
		],
	})
	assert [h.source for h in spec.post_extraction] == ['odb', 'dat']
