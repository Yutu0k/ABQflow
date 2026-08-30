"""End-to-end remote submission against a real machine.

Skipped unless ``--run-remote`` is passed and ``ABQFLOW_REMOTE_*`` names a
host.  The solve test additionally needs ``--run-abaqus``, since it consumes
license tokens on the far side.

These are the tests that would have caught, on a real machine, every problem
the remote spike found: an ``*INCLUDE`` rewritten to a path that does not
exist there, a hook whose interpreter is Python 2.7, a sidecar validated
before it was fetched, and a stale completion sentinel read from a previous
run.

Run: pytest test/integration/test_remote_submit.py -v --run-remote
Run (incl. a real solve): pytest test/integration/test_remote_submit.py -v --run-remote --run-abaqus
"""

from __future__ import annotations

import logging
import os

import pytest

from ABQflow.core.backends import make_backend
from ABQflow.core.context import JobContext
from ABQflow.core.diagnostics import diagnose
from ABQflow.core.runner import AbaqusRunner

pytestmark = pytest.mark.remote

_MINIMAL_INP = """\
*Heading
ABQflow remote integration probe
*Node
1, 0., 0., 0.
2, 1., 0., 0.
3, 1., 1., 0.
4, 0., 1., 0.
*Element, type=C3D8R
**
*Step, name=Probe
*Static
*End Step
"""


@pytest.fixture
def logger():
	log = logging.getLogger('test_remote_submit')
	log.addHandler(logging.NullHandler())
	return log


@pytest.fixture
def backend(remote_host, logger):
	be = make_backend(remote_host, logger=logger)
	yield be
	be.close()


@pytest.fixture
def job(tmp_path, remote_host, backend, logger):
	"""A runner wired to the real host, with a clean remote job directory."""
	name = 'abqflow_itest'
	local_dir = tmp_path / name
	local_dir.mkdir()
	ctx = JobContext(job_name=name, output_dir=str(local_dir), cpus=1)
	runner = AbaqusRunner(ctx, logger, backend=backend, host=remote_host)

	backend.makedirs(runner.exec_ctx.output_dir)
	yield runner

	for ext in ('.inp', '.abqflow.rc', '.abqflow.out', '.lck', '.sta',
				'.msg', '.dat', '.odb', '.log', '.com', '.prt', '.sim'):
		backend.remove(f'{runner.exec_ctx.output_dir}\\{ctx.job_name}{ext}')


# ============================================================
# connectivity and mapping
# ============================================================

def test_backend_reports_itself_remote(backend):
	assert backend.is_remote is True


def test_can_run_a_command_on_the_remote_machine(backend, remote_host):
	res = backend.run(['echo', 'ABQFLOW_OK'], remote_host.work_root, timeout=60)
	assert res.returncode == 0
	assert 'ABQFLOW_OK' in res.stdout


def test_abaqus_exe_is_present_on_the_remote_machine(backend, remote_host):
	"""The single most common misconfiguration: a bare 'abaqus'.

	A non-interactive SSH session inherits machine- and user-level
	environment but not what a login shell profile adds, and installers often
	put Abaqus on the installing user's PATH only.
	"""
	assert remote_host.abaqus_exe != 'abaqus', \
		"configure ABQFLOW_REMOTE_ABAQUS_EXE with an absolute path"
	assert backend.exists(remote_host.abaqus_exe)


def test_context_is_mapped_onto_the_remote_work_root(job, remote_host):
	assert job.exec_ctx.output_dir.startswith(remote_host.work_root)
	assert job.exec_ctx.abaqus_exe == remote_host.abaqus_exe


# ============================================================
# staging
# ============================================================

def test_inp_is_staged_onto_the_remote_machine(job, backend):
	with open(job.ctx.inp_path, 'w') as f:
		f.write(_MINIMAL_INP)
	assert job.stage_inputs() is True
	assert backend.exists(job.exec_ctx.inp_path)


def test_include_targets_travel_with_the_inp(job, backend):
	"""ExistingInpStrategy rewrites includes to *local* absolute paths."""
	parts = os.path.join(job.ctx.output_dir, 'parts')
	os.makedirs(parts, exist_ok=True)
	with open(os.path.join(parts, 'frag.inp'), 'w') as f:
		f.write('*Node\n9, 0., 0., 0.\n')
	with open(job.ctx.inp_path, 'w') as f:
		f.write('*Heading\n*INCLUDE, INPUT=parts/frag.inp\n*Step\n*End Step\n')

	assert job.stage_inputs() is True
	assert backend.exists(f'{job.exec_ctx.output_dir}\\frag.inp')
	remote_text = backend.read_text(job.exec_ctx.inp_path) or ''
	assert 'INPUT=frag.inp' in remote_text
	backend.remove(f'{job.exec_ctx.output_dir}\\frag.inp')


def test_transfers_are_byte_exact(job, backend, tmp_path):
	"""SFTP must not translate newlines: the .sta/.msg parsers read these."""
	payload = b'line one\r\nline two\r\n\x00binary'
	src = tmp_path / 'probe.bin'
	src.write_bytes(payload)
	remote = f'{job.exec_ctx.output_dir}\\probe.bin'

	backend.put(str(src), remote)
	back = tmp_path / 'back.bin'
	assert backend.get(remote, str(back)) is True
	assert back.read_bytes() == payload
	backend.remove(remote)


def test_stale_sentinels_are_cleared_before_a_launch(job, backend):
	"""A leftover .abqflow.rc made a job report success in zero seconds."""
	stale = f'{job.exec_ctx.output_dir}\\{job.ctx.job_name}.abqflow.rc'
	backend.put_text('99', stale)
	assert backend.exists(stale)
	backend.clear_sentinels(job.exec_ctx.output_dir, job.ctx.job_name)
	assert not backend.exists(stale)


# ============================================================
# hookkit under whatever Python the remote Abaqus ships
# ============================================================

def test_hookkit_imports_under_the_remote_abaqus_python(job, backend, remote_host):
	"""Abaqus 2022 and earlier ship Python 2.7.

	An f-string in hookkit.py made every extraction hook fail at import on
	such a machine while passing on a newer one.
	"""
	job._stage_hookkit()
	probe = f'{job.exec_ctx.output_dir}\\_hookkit_probe.py'
	backend.put_text(
		"import os, sys\n"
		"sys.path.insert(0, os.getcwd())\n"
		"import hookkit\n"
		"print('HOOKKIT_IMPORT_OK')\n",
		probe,
	)
	res = backend.run([remote_host.abaqus_exe, 'python', probe],
					job.exec_ctx.output_dir, timeout=600)
	backend.remove(probe)

	assert 'HOOKKIT_IMPORT_OK' in res.stdout, (
		"hookkit failed to import under the remote Abaqus interpreter.\n"
		f"stderr:\n{res.stderr[-800:]}"
	)


def test_hookkit_writes_a_csv_under_the_remote_interpreter(job, backend, remote_host):
	"""Py2's io.open text stream rejects byte strings.

	This surfaced as every CSV sidecar result becoming None, with the real
	cause only on stderr.
	"""
	job._stage_hookkit()
	probe = f'{job.exec_ctx.output_dir}\\_hookkit_csv_probe.py'
	backend.put_text(
		"import os, sys\n"
		"sys.path.insert(0, os.getcwd())\n"
		"import hookkit\n"
		"hookkit._write_csv([[1, 2.5]], ['a', 'b'], "
		"os.path.join(os.getcwd(), '_probe.csv'))\n"
		"print('CSV_OK')\n",
		probe,
	)
	res = backend.run([remote_host.abaqus_exe, 'python', probe],
					job.exec_ctx.output_dir, timeout=600)
	csv_remote = f'{job.exec_ctx.output_dir}\\_probe.csv'
	wrote = backend.exists(csv_remote)
	backend.remove(probe)
	backend.remove(csv_remote)

	assert 'CSV_OK' in res.stdout and wrote, (
		f"hookkit._write_csv failed remotely.\nstderr:\n{res.stderr[-800:]}"
	)


# ============================================================
# a real solve
# ============================================================

@pytest.mark.abaqus
def test_detached_solve_of_a_supplied_deck(job, backend, remote_host):
	"""Solve a real deck named by ``ABQFLOW_REMOTE_TEST_INP``.

	Kept opt-in because a meaningful solve needs a real model; the probe
	decks in ``examples/cae_file`` are the intended input.
	"""
	deck = os.environ.get('ABQFLOW_REMOTE_TEST_INP')
	if not deck or not os.path.isfile(deck):
		pytest.skip("set ABQFLOW_REMOTE_TEST_INP to a solvable .inp file")

	# Bytes, not text: the default codec follows the machine locale and a
	# UTF-8 BOM is enough to break it on a Chinese Windows.
	with open(deck, 'rb') as f:
		raw = f.read()
	if b'{{' in raw:
		pytest.skip("deck still contains {{placeholders}} — render it first")
	with open(job.ctx.inp_path, 'wb') as f:
		f.write(raw)

	result = job.run_solver()

	assert os.path.isfile(job.ctx.sta_path), "the .sta was not fetched back"
	diag = diagnose(job.ctx.job_name, job.ctx.output_dir)
	assert diag.sta_verdict == 'COMPLETED', f"solver said {diag.sta_verdict}"
	assert result.success is True
	assert not os.path.isfile(job.ctx.odb_path), \
		"the .odb should stay on the remote machine"
