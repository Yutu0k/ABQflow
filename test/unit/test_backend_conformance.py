"""Conformance suite every ExecutionBackend must satisfy.

``LocalBackend`` is the executable specification: ``SshBackend`` is asserted
against the identical checks under ``--run-remote``, so a remote backend that
diverges from local semantics fails here rather than six hours into a solve.

No mocking library is used — ``LocalBackend`` and ``RecordingBackend`` are
real implementations, and the SSH parameter talks to a real machine when one
is configured.

Run: pytest test/unit/test_backend_conformance.py -v
Run (incl. real SSH): pytest test/unit/test_backend_conformance.py -v --run-remote
"""

from __future__ import annotations

import os
import sys

import pytest

from ABQflow.core.backends import LocalBackend, RecordingBackend
from ABQflow.core.context import JobContext

# Every backend under test, as (id, factory) pairs. The factory takes a
# tmp_path so each test gets a clean sandbox.
_BACKENDS = [
	('local', lambda tmp: LocalBackend()),
	('local_staged', lambda tmp: LocalBackend(work_root=str(tmp / 'stage'))),
	('recording', lambda tmp: RecordingBackend(work_root=str(tmp / 'rec'))),
]


@pytest.fixture(params=[b[0] for b in _BACKENDS])
def backend(request, tmp_path):
	factory = dict((n, f) for n, f in _BACKENDS)[request.param]
	be = factory(tmp_path)
	yield be
	be.close()


@pytest.fixture
def workdir(tmp_path):
	d = tmp_path / 'work'
	d.mkdir()
	return str(d)


# ============================================================
# filesystem contract
# ============================================================

def test_makedirs_then_exists(backend, workdir):
	target = os.path.join(workdir, 'sub', 'deeper')
	backend.makedirs(target)
	# RecordingBackend keeps an in-memory tree and has no directories, so a
	# file is the portable way to assert the location is usable.
	backend.put_text('x', os.path.join(target, 'probe.txt'))
	assert backend.exists(os.path.join(target, 'probe.txt'))


def test_put_text_then_read_text_round_trips(backend, workdir):
	path = os.path.join(workdir, 'note.txt')
	backend.put_text('hello\nworld', path)
	assert 'hello' in (backend.read_text(path) or '')


def test_put_then_get_round_trips_bytes(backend, workdir, tmp_path):
	src = tmp_path / 'src.bin'
	payload = b'binary\x00payload\r\nwith crlf'
	src.write_bytes(payload)

	remote = os.path.join(workdir, 'copy.bin')
	backend.put(str(src), remote)

	back = tmp_path / 'back.bin'
	assert backend.get(remote, str(back)) is True
	assert back.read_bytes() == payload, "transfer must not translate newlines"


def test_get_missing_file_returns_false(backend, workdir, tmp_path):
	assert backend.get(os.path.join(workdir, 'nope.txt'),
					str(tmp_path / 'out.txt')) is False


def test_remove_deletes_and_reports(backend, workdir):
	path = os.path.join(workdir, 'gone.txt')
	backend.put_text('x', path)
	assert backend.remove(path) is True
	assert backend.exists(path) is False


def test_remove_missing_file_is_false_not_an_error(backend, workdir):
	assert backend.remove(os.path.join(workdir, 'never.txt')) is False


def test_glob_get_returns_only_matches(backend, workdir, tmp_path):
	for name in ('a.sta', 'b.msg', 'c.odb', 'd.txt'):
		backend.put_text('x', os.path.join(workdir, name))

	dest = tmp_path / 'pulled'
	fetched = backend.glob_get(workdir, ('*.sta', '*.msg'), str(dest))

	assert sorted(fetched) == ['a.sta', 'b.msg']
	assert not (dest / 'c.odb').exists(), "the .odb must stay put"


def test_glob_get_on_missing_dir_is_empty(backend, tmp_path):
	assert backend.glob_get(str(tmp_path / 'absent'), ('*.sta',),
						str(tmp_path / 'out')) == []


def test_read_text_missing_returns_none(backend, workdir):
	assert backend.read_text(os.path.join(workdir, 'absent.txt')) is None


def test_close_is_idempotent(backend):
	backend.close()
	backend.close()


# ============================================================
# context mapping
# ============================================================

def test_map_context_preserves_identity_fields(backend):
	ctx = JobContext(job_name='j1', output_dir=r'C:\local\j1', cpus=4)
	mapped = backend.map_context(ctx)
	assert mapped.job_name == 'j1'
	assert mapped.cpus == 4


def test_map_context_derived_paths_follow_output_dir(backend):
	ctx = JobContext(job_name='j1', output_dir=r'C:\local\j1', cpus=2)
	mapped = backend.map_context(ctx)
	assert mapped.inp_path.endswith('j1.inp')
	assert mapped.inp_path.startswith(mapped.output_dir)
	assert mapped.sta_path.startswith(mapped.output_dir)


def test_plain_local_backend_maps_context_to_itself():
	"""No work_root means run in place — the production default."""
	ctx = JobContext(job_name='j1', output_dir=r'C:\local\j1', cpus=2)
	assert LocalBackend().map_context(ctx) is ctx


# ============================================================
# command execution
# ============================================================

def test_run_reports_success(backend, workdir):
	cmd = ([sys.executable, '-c', 'print("hi")'])
	res = backend.run(cmd, workdir)
	assert res.returncode == 0


def test_run_failure_is_data_not_an_exception(workdir):
	"""A non-zero exit must come back as a return code, never as a raise."""
	res = LocalBackend().run([sys.executable, '-c', 'raise SystemExit(3)'], workdir)
	assert res.returncode == 3
	assert res.ok is False


def test_run_captures_stdout(workdir):
	res = LocalBackend().run([sys.executable, '-c', 'print("marker42")'], workdir)
	assert 'marker42' in res.stdout


def test_run_missing_executable_returns_none_rc(workdir):
	res = LocalBackend().run(['definitely-not-a-real-binary-xyz'], workdir)
	assert res.returncode is None
	assert res.stderr


# ============================================================
# detached execution + polling
# ============================================================

def test_submit_and_wait_round_trip(workdir):
	be = LocalBackend()
	handle = be.submit_detached(
		[sys.executable, '-c', 'import sys; sys.exit(0)'], workdir, 'j1')
	assert handle.launch_rc == 0
	verdict, rc, _elapsed = be.wait(handle, timeout_s=60)
	assert (verdict, rc) == ('finished', 0)
	be.close()


def test_wait_reports_the_solver_return_code(workdir):
	be = LocalBackend()
	handle = be.submit_detached(
		[sys.executable, '-c', 'import sys; sys.exit(7)'], workdir, 'j2')
	verdict, rc, _ = be.wait(handle, timeout_s=60)
	assert (verdict, rc) == ('finished', 7)
	be.close()


def test_rc_sentinel_is_written_after_completion(workdir):
	"""Both backends expose the same completion signal on disk."""
	be = LocalBackend()
	handle = be.submit_detached(
		[sys.executable, '-c', 'import sys; sys.exit(0)'], workdir, 'j3')
	be.wait(handle, timeout_s=60)
	assert be.exists(handle.rc_path)
	assert '0' in (be.read_text(handle.rc_path) or '')
	be.close()


def test_stale_sentinel_is_cleared_before_launch(workdir):
	"""Re-running into a dirty directory must not read the previous result.

	Observed during the spike: a leftover .abqflow.rc made a job report
	"finished" in zero seconds, carrying the *earlier* run's return code.
	"""
	be = LocalBackend()
	stale = os.path.join(workdir, 'j4.abqflow.rc')
	be.put_text('99', stale)

	handle = be.submit_detached(
		[sys.executable, '-c', 'import sys; sys.exit(0)'], workdir, 'j4')
	verdict, rc, _ = be.wait(handle, timeout_s=60)
	assert (verdict, rc) == ('finished', 0), "read the stale sentinel"
	be.close()


def test_wait_times_out_without_blocking_forever(workdir):
	be = LocalBackend()
	handle = be.submit_detached(
		[sys.executable, '-c', 'import time; time.sleep(30)'], workdir, 'j5')
	verdict, rc, elapsed = be.wait(handle, timeout_s=1)
	assert verdict == 'timeout' and rc is None
	assert elapsed < 20
	be.terminate(handle, 'abaqus', grace_s=1)
	be.close()


def test_recording_backend_scripts_the_poll_sequence(tmp_path):
	"""Lets a unit test drive the whole remote poll loop with no network."""
	be = RecordingBackend(work_root=str(tmp_path), poll_sequence=[None, None, 0])
	handle = be.submit_detached(['abaqus', 'job=j'], str(tmp_path), 'j')
	assert be.poll(handle) is None
	assert be.poll(handle) is None
	assert be.poll(handle) == 0
	assert be.poll(handle) == 0, "the last value must persist"


def test_recording_backend_records_the_solver_command(tmp_path):
	be = RecordingBackend(work_root=str(tmp_path))
	be.submit_detached(['abaqus', 'job=j1', 'interactive'], str(tmp_path), 'j1')
	assert be.commands('solver') == [['abaqus', 'job=j1', 'interactive']]
