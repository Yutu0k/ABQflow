"""The remote pipeline driven end to end without a network.

Two techniques, neither of which needs a mocking library:

* ``RecordingBackend`` — records commands and keeps an in-memory filesystem,
  so staging, launcher construction, the poll loop and the fetch ordering are
  all observable.
* ``LocalBackend(work_root=...)`` — a *real* filesystem at a *different*
  path, so path mapping and glob fetching run against real files.

Run: pytest test/unit/test_remote_pipeline.py -v
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

import pytest

from ABQflow.core.backends import LocalBackend, RecordingBackend
from ABQflow.core.context import JobContext
from ABQflow.core.hosts import HostSpec
from ABQflow.core.runner import AbaqusRunner


@pytest.fixture
def logger():
	log = logging.getLogger('test_remote_pipeline')
	log.addHandler(logging.NullHandler())
	return log


@pytest.fixture
def ctx_factory(tmp_path):
	"""Build a JobContext per job name, for tests spanning several jobs."""
	def _make(job_name: str = 'j1') -> JobContext:
		job_dir = tmp_path / 'local' / job_name
		job_dir.mkdir(parents=True, exist_ok=True)
		return JobContext(job_name=job_name, output_dir=str(job_dir), cpus=2,
						abaqus_exe='abaqus')
	return _make


@pytest.fixture
def ctx(ctx_factory):
	return ctx_factory('j1')


# ============================================================
# context mapping through the runner
# ============================================================

def test_local_runner_execution_context_is_the_original(ctx, logger):
	runner = AbaqusRunner(ctx, logger)
	assert runner.exec_ctx is ctx
	assert runner.is_remote is False


def test_remote_runner_maps_paths_and_exe(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	assert runner.is_remote is True
	assert runner.exec_ctx.output_dir != ctx.output_dir
	assert runner.exec_ctx.inp_path.endswith('j1.inp')


def test_solver_command_is_built_against_the_remote_context(ctx, logger, tmp_path):
	"""The builders themselves are untouched; only their input changes."""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	cmd = runner.build_solver_command(runner.exec_ctx)
	assert any(a.startswith('input=') and str(tmp_path / 'remote') in a for a in cmd)
	assert 'interactive' in cmd


# ============================================================
# artifact_exists — the silent-failure guard
# ============================================================

def test_artifact_exists_local_reads_the_local_disk(ctx, logger):
	runner = AbaqusRunner(ctx, logger)
	assert runner.artifact_exists(ctx.odb_path) is False
	open(ctx.odb_path, 'wb').close()
	assert runner.artifact_exists(ctx.odb_path) is True


def test_artifact_exists_remote_asks_the_executing_machine(ctx, logger, tmp_path):
	"""A local .odb must not make a remote extraction think it can proceed —
	and a remote one must be visible even though nothing is local."""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	open(ctx.odb_path, 'wb').close()          # exists locally only
	assert runner.artifact_exists(ctx.odb_path) is False

	backend.put_text('odb', runner.exec_ctx.odb_path)
	assert runner.artifact_exists(ctx.odb_path) is True


# ============================================================
# staging
# ============================================================

def test_stage_inputs_uploads_the_inp(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	with open(ctx.inp_path, 'w') as f:
		f.write('*Heading\n*Step\n*End Step\n')

	assert runner.stage_inputs() is True
	assert backend.exists(runner.exec_ctx.inp_path)


def _write_deck_with_include(ctx, mesh_body='*Node\n1, 0., 0.\n',
							rel='parts/mesh.inp'):
	"""Write an INP referencing *rel*, and the target itself."""
	target = os.path.join(ctx.output_dir, rel.replace('/', os.sep))
	os.makedirs(os.path.dirname(target), exist_ok=True)
	with open(target, 'w') as f:
		f.write(mesh_body)
	with open(ctx.inp_path, 'w') as f:
		f.write(f'*Heading\n*INCLUDE, INPUT={rel}\n*End\n')
	return target


def test_stage_inputs_rewrites_includes_to_the_shared_dir(ctx, logger, tmp_path):
	"""The concrete blocker: includes rewritten to local absolute paths
	cannot exist on the far machine.

	They are rewritten to an absolute path in the machine's shared directory
	rather than copied beside each deck — a referenced mesh is often far
	larger than the deck itself.
	"""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	_write_deck_with_include(ctx)

	assert runner.stage_inputs() is True
	uploaded = backend.read_text(runner.exec_ctx.inp_path) or ''

	assert '_abqflow_shared' in uploaded
	assert 'mesh.inp' in uploaded
	assert not backend.exists(runner.exec_ctx.output_dir + '\\mesh.inp'), \
		"the target must not be duplicated into the job directory"


def test_include_target_is_uploaded_once_across_jobs(ctx_factory, logger, tmp_path):
	"""A shared mesh must not be re-sent for every job in a sweep."""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))

	uploads = []
	original_put = backend.put

	def counting_put(local, remote):
		uploads.append(remote)
		return original_put(local, remote)

	backend.put = counting_put

	for i in range(3):
		job_ctx = ctx_factory(f'sweep_{i:02d}')
		_write_deck_with_include(job_ctx)
		runner = AbaqusRunner(job_ctx, logger, backend=backend)
		assert runner.stage_inputs() is True

	mesh_uploads = [u for u in uploads if 'mesh.inp' in u]
	assert len(mesh_uploads) == 1, f"uploaded {len(mesh_uploads)} times: {mesh_uploads}"


def test_same_basename_different_content_do_not_collide(ctx_factory, logger, tmp_path):
	"""Content addressing keeps two different 'mesh.inp' files apart."""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	remotes = []

	for i, body in enumerate(('*Node\n1, 0., 0.\n', '*Node\n2, 1., 1.\n')):
		job_ctx = ctx_factory(f'variant_{i}')
		_write_deck_with_include(job_ctx, mesh_body=body)
		runner = AbaqusRunner(job_ctx, logger, backend=backend)
		assert runner.stage_inputs() is True
		remotes.append(backend.read_text(runner.exec_ctx.inp_path) or '')

	assert remotes[0] != remotes[1], "different content must map to different names"


def test_shared_dir_sits_beside_the_job_dirs(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	shared = runner._shared_dir()
	assert shared.endswith('_abqflow_shared')
	assert not shared.startswith(runner.exec_ctx.output_dir)


def test_stage_inputs_fails_loudly_on_a_missing_include(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	with open(ctx.inp_path, 'w') as f:
		f.write('*INCLUDE, INPUT=absent/mesh.inp\n')
	assert runner.stage_inputs() is False


def test_stage_inputs_is_a_noop_locally(ctx, logger):
	assert AbaqusRunner(ctx, logger).stage_inputs() is None


# ============================================================
# fetching results
# ============================================================

def test_fetch_results_pulls_small_files_and_leaves_the_odb(ctx, logger, tmp_path):
	host = HostSpec(name='n1', hostname='h', work_root=r'D:\w', abaqus_exe=r'C:\abq\abaqus.bat')
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend, host=host)

	for name in ('j1.sta', 'j1.msg', 'j1.dat', 'j1.odb'):
		backend.put_text('x', f'{runner.exec_ctx.output_dir}\\{name}')

	fetched = runner.fetch_results()
	assert sorted(fetched) == ['j1.dat', 'j1.msg', 'j1.sta']
	assert not os.path.exists(os.path.join(ctx.output_dir, 'j1.odb'))


def test_fetch_odb_when_the_host_asks_for_it(ctx, logger, tmp_path):
	host = HostSpec(name='n1', hostname='h', work_root=r'D:\w', abaqus_exe=r'C:\abq\abaqus.bat', fetch_odb=True)
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend, host=host)
	backend.put_text('x', f'{runner.exec_ctx.output_dir}\\j1.odb')

	assert 'j1.odb' in runner.fetch_results()


def test_fetch_results_is_a_noop_locally(ctx, logger):
	assert AbaqusRunner(ctx, logger).fetch_results() == []


# ============================================================
# compile-cache marker is per machine
# ============================================================

def test_compile_marker_is_namespaced_by_host(ctx, logger, tmp_path):
	"""Compiling on one machine must not make another skip its own build."""
	from ABQflow.core.spec import SubroutineSpec

	src = tmp_path / 'umat.f'
	src.write_text('      subroutine umat\n      end\n')
	sub = SubroutineSpec(source_path=str(src))

	local_marker = AbaqusRunner(ctx, logger)._compile_hash_path(sub)
	remote_marker = AbaqusRunner(
		ctx, logger,
		backend=RecordingBackend(work_root=str(tmp_path / 'r'), name='node02'),
	)._compile_hash_path(sub)

	assert local_marker != remote_marker
	assert 'local' in os.path.basename(local_marker)
	assert 'node02' in os.path.basename(remote_marker)


# ============================================================
# LocalBackend as a real "fake remote"
# ============================================================

def test_full_staging_round_trip_on_a_real_filesystem(ctx, logger, tmp_path):
	"""Stage up, produce artifacts, fetch back — real files, no network.

	This is where path-joining and fetch-glob bugs actually surface.
	"""
	stage_root = tmp_path / 'stage'
	host = HostSpec(name='fake', hostname='h', work_root=str(stage_root), abaqus_exe=r'C:\abq\abaqus.bat')
	backend = LocalBackend(work_root=str(stage_root))
	runner = AbaqusRunner(ctx, logger, backend=backend, host=host)

	with open(ctx.inp_path, 'w') as f:
		f.write('*Heading\n*Step\n*End Step\n')
	assert runner.stage_inputs() is True

	remote_dir = runner.exec_ctx.output_dir
	assert os.path.isfile(os.path.join(remote_dir, 'j1.inp'))

	# Pretend the solver ran.
	with open(os.path.join(remote_dir, 'j1.sta'), 'w') as f:
		f.write(' THE ANALYSIS HAS COMPLETED SUCCESSFULLY\n')
	with open(os.path.join(remote_dir, 'j1.odb'), 'wb') as f:
		f.write(b'0' * 4096)

	fetched = runner.fetch_results()
	assert 'j1.sta' in fetched
	assert os.path.isfile(os.path.join(ctx.output_dir, 'j1.sta'))
	assert not os.path.isfile(os.path.join(ctx.output_dir, 'j1.odb'))

	# The unchanged diagnostics module reads the staged-back files.
	from ABQflow.core.diagnostics import diagnose
	assert diagnose('j1', ctx.output_dir).sta_verdict == 'COMPLETED'


# ============================================================
# hook argument mapping and interpreter choice
# ============================================================

def test_hook_common_args_are_mapped_to_the_executing_machine(ctx, logger, tmp_path):
	"""``--odb_path`` must name the artifact where the hook can actually see it.

	Passing the local path made the remote hook fail to open the ODB.  hookkit
	turns each task failure into ``None`` and still exits 0, so the job
	reported EXTRACTION_FAILED with nothing in the log explaining it.
	"""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	script = tmp_path / 'hook.py'
	script.write_text('print("x")\n')
	runner.run_hook(str(script), [{'result_name': 'r'}],
					{'--odb_path': ctx.odb_path}, needs_cae_kernel=False)

	cmd = backend.commands('hook')[0] if backend.commands('hook') else backend.commands('run')[0]
	odb_arg = cmd[cmd.index('--odb_path') + 1]
	assert odb_arg.startswith(runner.exec_ctx.output_dir)
	assert not odb_arg.startswith(ctx.output_dir)


def test_hook_args_outside_the_job_dir_are_left_alone(ctx, logger, tmp_path):
	"""Only paths we own get rewritten; anything else is not ours to guess at."""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	assert runner._remote_path(r'D:\shared\reference.csv') == r'D:\shared\reference.csv'


def test_remote_hooks_never_use_a_bare_python(ctx, logger, tmp_path):
	"""abqpy on *this* machine says nothing about the remote one.

	If the local environment had abqpy, build_script_command would otherwise
	pick ``python <script>`` — which on the far machine is the system Python,
	with no odbAccess.
	"""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)
	runner._has_abqpy = True                      # pretend it is installed here

	script = tmp_path / 'hook.py'
	script.write_text('print("x")\n')
	runner.run_hook(str(script), [{'result_name': 'r'}],
					{'--odb_path': ctx.odb_path}, needs_cae_kernel=False)

	cmd = (backend.commands('hook') or backend.commands('run'))[0]
	assert cmd[0] == runner.exec_ctx.abaqus_exe
	assert cmd[1] == 'python', "must go through the remote machine's abaqus python"


def test_cae_kernel_hook_uses_the_remote_abaqus_cae(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	script = tmp_path / 'hook.py'
	script.write_text('print("x")\n')
	runner.run_hook(str(script), [{'result_name': 'r'}],
					{'--inp_path': ctx.inp_path}, needs_cae_kernel=True)

	cmd = (backend.commands('hook') or backend.commands('run'))[0]
	assert cmd[0] == runner.exec_ctx.abaqus_exe
	assert cmd[1] == 'cae' and cmd[2].startswith('noGUI=')


# ============================================================
# preparation stays on this machine
# ============================================================

def test_preparation_commands_run_locally(ctx, logger, tmp_path):
	"""INP generation happens here, then the finished deck is shipped.

	Routing a preparation command to the remote backend would run this
	machine's abaqus_exe path over there, where it does not exist.
	"""
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	runner._run([sys.executable, '-c', 'pass'], stage='preparation')
	assert backend.command_log == [], "preparation must not touch the remote backend"


def test_hook_commands_still_run_remotely(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	runner._run(['abaqus', 'python', 'x.py'], stage='hook')
	assert len(backend.command_log) == 1


def test_on_local_can_be_forced_either_way(ctx, logger, tmp_path):
	backend = RecordingBackend(work_root=str(tmp_path / 'remote'))
	runner = AbaqusRunner(ctx, logger, backend=backend)

	runner._run(['abaqus', 'x'], stage='preparation', on_local=False)
	assert len(backend.command_log) == 1, "explicit on_local=False must win"


def test_local_runner_has_no_separate_local_backend(ctx, logger):
	"""Running locally, the two are the same object — no extra indirection."""
	runner = AbaqusRunner(ctx, logger)
	assert runner.local_backend is runner.backend


# ============================================================
# per-host concurrency gate
# ============================================================

def test_gated_worker_enforces_per_host_concurrency():
	"""The mechanism behind "two jobs to A, one to B".

	Runs the gate directly: a semaphore of 2 must never let a third caller
	in while two are inside.
	"""
	from ABQflow.core.abaqus_automation import _gated_worker

	gate = threading.Semaphore(2)
	inside = []
	peak = []
	lock = threading.Lock()

	class _Calc:
		job_name = 'j'

		def execute(self, phase='full'):
			with lock:
				inside.append(1)
				peak.append(len(inside))
			time.sleep(0.05)
			with lock:
				inside.pop()
			return {'status': 'COMPLETED'}

		class ctx:
			output_dir = '.'

	threads = [threading.Thread(target=_gated_worker, args=(_Calc(), 'full', gate))
			for _ in range(6)]
	for t in threads:
		t.start()
	for t in threads:
		t.join()

	assert max(peak) <= 2, f"gate allowed {max(peak)} concurrent jobs, cap was 2"


def test_gated_worker_releases_the_slot_on_failure():
	"""A broken machine must not leak slots and stall the batch."""
	from ABQflow.core.abaqus_automation import _gated_worker

	gate = threading.Semaphore(1)

	class _Boom:
		job_name = 'boom'

		def execute(self, phase='full'):
			raise RuntimeError('remote machine is down')

		class ctx:
			output_dir = '.'

	for _ in range(3):
		outcome = _gated_worker(_Boom(), 'full', gate)
		assert outcome.status == 'UNKNOWN_ERROR'
	assert gate.acquire(blocking=False), "slot was leaked"
