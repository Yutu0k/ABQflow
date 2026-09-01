"""Execution backends — where a job's commands actually run.

``ssh`` is imported lazily by :func:`make_backend`, never at module scope, so
a local-only installation without ``paramiko`` can still ``import ABQflow``.
"""

from __future__ import annotations

from .base import ExecResult, ExecutionBackend, JobHandle
from .local import LocalBackend, wait_for
from .recording import RecordingBackend

__all__ = [
	'ExecResult',
	'ExecutionBackend',
	'JobHandle',
	'LocalBackend',
	'RecordingBackend',
	'make_backend',
	'wait_for',
]


def make_backend(host=None, logger=None) -> ExecutionBackend:
	"""Build the backend for *host*.

	``None``, or a :class:`~ABQflow.core.hosts.HostSpec` with no hostname,
	yields a :class:`LocalBackend` — so the remote code path is unreachable
	unless a remote host is explicitly configured.

	Parameters
	----------
	host : HostSpec or None
		Target machine.
	logger : logging.Logger or None
		Forwarded to remote backends for connection logging.
	"""
	if host is None:
		return LocalBackend()
	if not getattr(host, 'hostname', None):
		# A local HostSpec: still a pool member, but everything runs here.
		return LocalBackend(host=host)

	from .ssh import SshBackend       # lazy: pulls in paramiko
	return SshBackend(host, logger=logger)
