"""HostSpec capacity/weight semantics and multi-machine job assignment.

Two knobs are deliberately separate here, and most of these tests exist to
keep them that way:

* ``max_concurrent`` — how many jobs run on a machine **at once**
* ``weight``         — what **share of the batch** a machine receives

Conflating them was a real risk: of two machines measured during the remote
spike, the one with half the cores finished the same job 60% faster, so
ranking by core count alone sends most of the work to the slower machine.

Run: pytest test/unit/test_hosts.py -v
"""

from __future__ import annotations

import pytest

from ABQflow.core.hosts import (
	HostSpec,
	assign_hosts,
	solver_tokens,
	summarise_assignment,
	total_capacity,
)


def _remote(name, **kw):
	"""A valid remote host; work_root is mandatory for those."""
	kw.setdefault('hostname', f'{name}.example')
	kw.setdefault('work_root', r'D:\abqwork')
	return HostSpec(name=name, **kw)


# ============================================================
# solver_tokens
# ============================================================

def test_solver_tokens_known_values():
	assert solver_tokens(1) == 5
	assert solver_tokens(2) == 7
	assert solver_tokens(4) == 9


def test_solver_tokens_is_sublinear():
	"""Widening a job costs far fewer tokens than running more jobs.

	This is the arithmetic behind preferring a larger cpus_per_job over more
	concurrency when licences are the binding constraint.
	"""
	seq = [solver_tokens(n) for n in (1, 2, 4, 8, 16)]
	assert seq == sorted(seq) and len(set(seq)) == len(seq)
	assert seq[-1] < 5 * 16


def test_solver_tokens_floors_at_one_cpu():
	assert solver_tokens(0) == solver_tokens(1)


# ============================================================
# capacity
# ============================================================

def test_capacity_from_cores():
	host = _remote('a', cpus_total=32, cpus_per_job=2)
	assert host.capacity() == (32 - 1) // 2


def test_capacity_respects_explicit_max_concurrent():
	"""An explicit answer always wins over the derived one."""
	host = _remote('a', cpus_total=32, cpus_per_job=2, max_concurrent=2)
	assert host.capacity() == 2


def test_capacity_treats_tokens_as_a_hard_cap():
	"""Tokens cap concurrency; cores only ever slow things down."""
	host = _remote('a', cpus_total=32, cpus_per_job=2, license_tokens=21)
	assert host.capacity() == 21 // solver_tokens(2) == 3


def test_capacity_is_at_least_one():
	host = _remote('a', cpus_total=1, cpus_per_job=8, license_tokens=1)
	assert host.capacity() == 1


def test_capacity_uses_batch_cpus_when_host_has_none():
	host = _remote('a', cpus_total=16)
	assert host.capacity(batch_cpus_per_job=4) == (16 - 1) // 4


def test_host_cpus_override_batch_default():
	host = _remote('a', cpus_per_job=8)
	assert host.resolved_cpus(2) == 8
	assert _remote('b').resolved_cpus(2) == 2


# ============================================================
# weight
# ============================================================

def test_weight_defaults_to_capacity():
	host = _remote('a', cpus_total=32, cpus_per_job=2)
	assert host.allocation_weight() == float(host.capacity())


def test_explicit_weight_overrides_capacity():
	"""The knob that lets a fast-but-small machine take more of the batch."""
	host = _remote('a', cpus_total=8, cpus_per_job=2, weight=10.0)
	assert host.capacity() == 3
	assert host.allocation_weight() == 10.0


def test_weight_and_max_concurrent_are_independent():
	"""A machine can be given a large share but still run one job at a time."""
	host = _remote('a', cpus_total=32, max_concurrent=1, weight=5.0)
	assert host.capacity() == 1
	assert host.allocation_weight() == 5.0


# ============================================================
# validation
# ============================================================

def test_remote_host_requires_work_root():
	with pytest.raises(ValueError, match='work_root'):
		HostSpec(name='a', hostname='a.example')


def test_local_host_needs_no_work_root():
	assert HostSpec(name='local').is_remote is False


def test_rejects_bad_cleanup_mode():
	with pytest.raises(ValueError, match='cleanup'):
		_remote('a', cleanup='sometimes')


def test_rejects_non_positive_weight():
	with pytest.raises(ValueError, match='weight'):
		_remote('a', weight=0)


def test_rejects_zero_max_concurrent():
	with pytest.raises(ValueError, match='max_concurrent'):
		_remote('a', max_concurrent=0)


def test_rejects_empty_name():
	with pytest.raises(ValueError, match='name'):
		HostSpec(name='')


# ============================================================
# assign_hosts
# ============================================================

def test_single_host_is_the_identity_assignment():
	"""What lets the remote path be adopted without changing single-machine runs."""
	host = _remote('a', cpus_total=32)
	jobs = [f'j{i}' for i in range(7)]
	grouped = summarise_assignment(assign_hosts(jobs, [host]))
	assert grouped == {'a': jobs}


def test_assignment_is_proportional_to_capacity():
	big = _remote('big', cpus_total=32, cpus_per_job=2)     # capacity 15
	small = _remote('small', cpus_total=8, cpus_per_job=2)  # capacity 3
	grouped = summarise_assignment(
		assign_hosts([f'j{i:02d}' for i in range(18)], [big, small]))
	assert len(grouped['big']) == 15
	assert len(grouped['small']) == 3


def test_explicit_weight_beats_core_count():
	"""The measured case: fewer cores, but faster, so give it more work."""
	slow = _remote('slow', cpus_total=32, cpus_per_job=2)
	fast = _remote('fast', cpus_total=16, cpus_per_job=2, weight=30.0)
	grouped = summarise_assignment(
		assign_hosts([f'j{i:02d}' for i in range(20)], [slow, fast]))
	assert len(grouped['fast']) > len(grouped['slow'])


def test_every_job_assigned_exactly_once():
	hosts = [_remote('a', cpus_total=8), _remote('b', cpus_total=16)]
	jobs = [f'j{i}' for i in range(13)]
	assignment = assign_hosts(jobs, hosts)
	assert set(assignment) == set(jobs)
	assert sum(len(v) for v in summarise_assignment(assignment).values()) == 13


def test_equal_weights_round_robin_in_config_order():
	a, b = _remote('a', max_concurrent=2), _remote('b', max_concurrent=2)
	assignment = assign_hosts(['j1', 'j2', 'j3', 'j4'], [a, b])
	assert [assignment[j].name for j in ('j1', 'j2', 'j3', 'j4')] == \
		['a', 'b', 'a', 'b']


def test_empty_job_list_is_allowed():
	assert assign_hosts([], [_remote('a')]) == {}


def test_rejects_empty_host_pool():
	with pytest.raises(ValueError, match='at least one host'):
		assign_hosts(['j1'], [])


def test_rejects_duplicate_host_names():
	"""Two machines sharing a name would silently merge in the assignment."""
	with pytest.raises(ValueError, match='Duplicate'):
		assign_hosts(['j1'], [_remote('a'), _remote('a')])


def test_total_capacity_sums_hosts():
	hosts = [_remote('a', max_concurrent=2), _remote('b', max_concurrent=1)]
	assert total_capacity(hosts) == 3


def test_job_dir_is_under_work_root():
	host = _remote('a', work_root='D:\\abqwork\\')
	assert host.job_dir('myjob') == 'D:\\abqwork\\myjob'
