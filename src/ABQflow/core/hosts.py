"""HostSpec — a machine that can execute Abaqus jobs, and how work is spread over several.

A batch with no host runs locally, exactly as it always has.  Supplying one or
more :class:`HostSpec` objects is the only way to reach the remote code path,
so remote execution is strictly opt-in.

Two independent knobs control multi-machine behaviour, and conflating them is
the mistake this module exists to prevent:

``max_concurrent``
	How many jobs may run on a machine **at the same time**.  A capacity
	limit, enforced by a semaphore during execution.

``weight``
	What **share of the batch** a machine should receive.  A throughput
	preference, applied when jobs are assigned.

They are not the same thing, and measurement showed why: of two machines
tested, the one with half the cores finished the same job 60% faster.  Ranking
purely by core count would have sent most of the work to the slower machine.
So ``weight`` defaults to ``max_concurrent`` (a reasonable proxy) but can be
set explicitly once you know how fast a machine actually is.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def physical_cores() -> int:
	"""Physical core count of *this* machine, with a safe fallback.

	``psutil`` is preferred; without it the count is of *logical* processors,
	which over-estimates on an SMT machine.  Core count is only ever advisory
	(licence tokens are the hard cap), so erring high is harmless.
	"""
	try:
		import psutil
		count = psutil.cpu_count(logical=False)
		if count:
			return count
	except Exception:
		pass
	import os
	return os.cpu_count() or 1


def solver_tokens(n_cpus: int) -> int:
	"""Estimate Abaqus license tokens needed for *n_cpus* cores.

	``ceil(5 * n ** 0.422)`` — re-exported from
	:mod:`~ABQflow.core.abaqus_automation` semantics so this module can be
	read on its own.  Sublinear: 1→5, 2→7, 4→9, 8→13, 16→17 tokens, which
	means widening a job costs far fewer tokens than running more jobs.
	"""
	return math.ceil(5 * max(1, n_cpus) ** 0.422)


@dataclass(frozen=True)
class HostSpec:
	"""A machine that can execute Abaqus jobs.

	Attributes
	----------
	name : str
		Short identifier used in logs, in per-host cache-marker filenames,
		and to namespace fetched results.  Must be unique within a batch.
	hostname : str or None
		Hostname, or a **bare** IPv6/IPv4 literal — no square brackets, since
		this goes straight to :func:`socket.getaddrinfo`.  A Windows
		link-local address needs a *numeric* zone index (``fe80::1%12``), not
		an interface name.  ``None`` designates the local machine.
	username, password : str or None
		Password authentication.  Prefer sourcing these from the environment;
		never commit them.
	key_filename : str or None
		Private-key path.  Takes precedence over *password* when set.
	port : int
		SSH port.
	abaqus_exe : str
		**Absolute** path to ``abaqus.bat`` on this machine.  Empty means
		"inherit the batch default", which is what a local host normally
		wants; a remote host must set it, and the constructor refuses one
		that does not.

		A bare ``'abaqus'`` frequently fails under a non-interactive SSH
		session, which inherits machine- and user-level environment but not
		whatever a login shell profile adds — and installers often put Abaqus
		on the installing user's PATH only.  Failing at construction beats
		discovering it after the batch has been dispatched.
	work_root : str
		Directory under which per-job working directories are created.
	cpus_per_job : int or None
		``cpus=`` for jobs sent here.  ``None`` inherits the batch default,
		which is what keeps a homogeneous batch simple.
	cpus_total : int or None
		Physical cores.  Used to derive :meth:`capacity` when
		*max_concurrent* is not given.
	license_tokens : int or None
		Token budget for this machine.  A **hard** cap on concurrency —
		Abaqus refuses to start a job it cannot license, whereas
		oversubscribing cores merely slows things down.
	max_concurrent : int or None
		Explicit cap on simultaneous jobs here.  ``None`` derives one from
		cores and tokens.  This is what lets a batch send, say, two jobs to
		one machine and one to another.
	weight : float or None
		Share of the batch this machine should receive, relative to the
		others.  ``None`` falls back to :meth:`capacity`, so machines are
		fed in proportion to how much they can run at once.  Set it
		explicitly when a machine is faster per job than its core count
		suggests.
	reserve_cores : int
		Cores left free for the OS when deriving capacity.
	max_sessions : int
		Cap on concurrent SSH connections; keep below sshd's ``MaxSessions``
		(default 10), above which refusals look like random network errors.
	fetch_globs : tuple[str, ...]
		Filename patterns pulled back after each phase.
	fetch_odb : bool
		Also retrieve the ``.odb``.  Default ``False``: ODBs are frequently
		multi-gigabyte and leaving them where the solver wrote them is the
		point of running remotely.
	cleanup : str
		``'never'`` (default) | ``'on_success'`` | ``'always'``.  Defaults to
		keeping everything, because a remote job whose directory was deleted
		cannot be debugged.
	connect_timeout : float
		Seconds to wait for the SSH handshake.
	poll_interval : float
		Initial seconds between completion polls; backs off to
		*poll_max_interval*.
	poll_max_interval : float
		Ceiling for the poll backoff.
	"""

	name: str = 'local'
	hostname: str | None = None
	username: str | None = None
	password: str | None = None
	key_filename: str | None = None
	port: int = 22
	abaqus_exe: str = ''
	work_root: str = ''
	cpus_per_job: int | None = None
	cpus_total: int | None = None
	license_tokens: int | None = None
	max_concurrent: int | None = None
	weight: float | None = None
	reserve_cores: int = 1
	max_sessions: int = 8
	fetch_globs: tuple[str, ...] = (
		'*.sta', '*.msg', '*.dat', '*.log', '*.csv', '*.abqflow.*',
	)
	fetch_odb: bool = False
	cleanup: str = 'never'
	connect_timeout: float = 20.0
	poll_interval: float = 2.0
	poll_max_interval: float = 30.0
	meta: dict = field(default_factory=dict)

	def __post_init__(self):
		if not self.name:
			raise ValueError("HostSpec.name must be a non-empty string")
		if self.cleanup not in ('never', 'on_success', 'always'):
			raise ValueError(
				f"HostSpec.cleanup must be 'never', 'on_success' or 'always', "
				f"got {self.cleanup!r}"
			)
		if self.max_concurrent is not None and self.max_concurrent < 1:
			raise ValueError("HostSpec.max_concurrent must be >= 1 when given")
		if self.weight is not None and self.weight <= 0:
			raise ValueError("HostSpec.weight must be > 0 when given")
		if self.is_remote and not self.work_root:
			raise ValueError(
				f"HostSpec {self.name!r} is remote but has no work_root — "
				"per-job directories have nowhere to go"
			)
		if self.is_remote and not self.abaqus_exe:
			raise ValueError(
				f"HostSpec {self.name!r} is remote but has no abaqus_exe. "
				"Give the absolute path to abaqus.bat on that machine: a "
				"non-interactive SSH session does not inherit the installing "
				"user's PATH, so a bare 'abaqus' usually fails there."
			)

	@classmethod
	def local(cls, name: str = 'local', **kwargs) -> HostSpec:
		"""This machine, as a pool member.

		Mixing it with remote hosts lets a batch use the submitting machine's
		spare capacity alongside the others::

			hosts = [
				HostSpec.local(max_concurrent=1),   # keep this machine usable
				HostSpec(name='node01', hostname='NODE01', ...),
			]

		Everything a local host runs goes through :class:`LocalBackend`, so
		it behaves exactly as a host-less batch does — no staging, no fetch,
		no SSH.  ``cpus_total`` is measured if not given.
		"""
		kwargs.pop('hostname', None)
		return cls(name=name, hostname=None, **kwargs)

	@property
	def is_remote(self) -> bool:
		"""Whether this spec designates a machine reached over SSH."""
		return bool(self.hostname)

	@property
	def shared_dir(self) -> str:
		"""Directory holding files reused across this machine's jobs.

		``*INCLUDE`` targets land here rather than in each job directory: a
		shared mesh is often far larger than the deck that references it, and
		uploading it once per job would make transfer, not solving, the
		dominant cost of a batch.  Contents are content-addressed, so they
		survive between batches and are never invalidated by a stale name.

		Not touched by ``cleanup``, which only ever removes job directories —
		this is a cache, and keeping it is the whole point.
		"""
		return self.work_root.rstrip('\\/') + '\\_abqflow_shared'

	def job_dir(self, job_name: str) -> str:
		"""Working directory for *job_name* on this machine.

		Windows path separators: both ends of the tested deployment are
		Windows.  Joining is deliberately confined to this method so a
		POSIX target later needs one change, not a search across the package.
		"""
		return self.work_root.rstrip('\\/') + '\\' + job_name

	def resolved_cpus(self, batch_default: int) -> int:
		"""``cpus=`` for jobs on this machine, falling back to the batch value."""
		return self.cpus_per_job if self.cpus_per_job else batch_default

	def resolved_abaqus_exe(self, batch_default: str) -> str:
		"""Abaqus command for this machine, falling back to the batch value.

		A remote host always has its own (the constructor insists on it); a
		local host normally inherits whatever the batch was configured with.
		"""
		return self.abaqus_exe or batch_default

	def capacity(self, batch_cpus_per_job: int = 1) -> int:
		"""How many jobs may run here at once.

		Resolution order:

		1. *max_concurrent*, when set — an explicit answer always wins.
		2. Otherwise derived from cores and tokens, with tokens as a hard cap
		   and cores advisory.

		Always at least 1: a machine worth configuring can run one job.
		"""
		if self.max_concurrent is not None:
			return self.max_concurrent

		cpus = self.resolved_cpus(batch_cpus_per_job)

		# For this machine the core count is knowable, so an unset cpus_total
		# is measured rather than assumed — otherwise the local host would
		# fall back to a capacity of 1 and be starved next to the remotes.
		total = self.cpus_total
		if total is None and not self.is_remote:
			total = physical_cores()

		by_cores = 1
		if total:
			by_cores = max(1, (total - self.reserve_cores) // max(1, cpus))

		if self.license_tokens is None:
			return by_cores
		by_tokens = max(1, self.license_tokens // solver_tokens(cpus))
		return max(1, min(by_cores, by_tokens))

	def allocation_weight(self, batch_cpus_per_job: int = 1) -> float:
		"""Share of the batch this machine should receive.

		An explicit *weight* wins.  Otherwise the machine's concurrency
		capacity is used, so by default work is spread in proportion to how
		much each machine can run at once.
		"""
		if self.weight is not None:
			return float(self.weight)
		return float(self.capacity(batch_cpus_per_job))


LOCAL_HOST = HostSpec(name='local')
"""The default: run everything on this machine, exactly as before."""


@dataclass
class HostAssignment:
	"""One job placed on one machine."""

	job_name: str
	host: HostSpec


def assign_hosts(job_names: list[str], hosts: list[HostSpec],
				batch_cpus_per_job: int = 1) -> dict[str, HostSpec]:
	"""Distribute *job_names* over *hosts* in proportion to their weights.

	Jobs are dealt out one at a time to whichever machine is currently
	*least loaded relative to its own weight*, so a machine with weight 3
	receives roughly three times as much work as one with weight 1.  With a
	single host this is the identity assignment, which is what lets the
	remote path be adopted without changing single-machine behaviour.

	Weights come from :meth:`HostSpec.allocation_weight`: an explicit
	``weight`` if configured, otherwise the machine's concurrency capacity.

	Parameters
	----------
	job_names : list[str]
		Jobs to place, in order.
	hosts : list[HostSpec]
		Candidate machines; must be non-empty and uniquely named.
	batch_cpus_per_job : int
		Batch default used when a host does not set its own ``cpus_per_job``.

	Returns
	-------
	dict[str, HostSpec]
		``{job_name: host}`` for every job.

	Raises
	------
	ValueError
		If *hosts* is empty, or two hosts share a name — silently dropping
		jobs or merging machines would both be worse than failing here.
	"""
	if not hosts:
		raise ValueError("assign_hosts() needs at least one host")

	names = [h.name for h in hosts]
	duplicates = sorted({n for n in names if names.count(n) > 1})
	if duplicates:
		raise ValueError(f"Duplicate HostSpec.name in host pool: {duplicates}")

	weights = {h.name: h.allocation_weight(batch_cpus_per_job) for h in hosts}
	load = {h.name: 0.0 for h in hosts}
	order = {h.name: i for i, h in enumerate(hosts)}

	out: dict[str, HostSpec] = {}
	for job_name in job_names:
		best = min(hosts, key=lambda h: (load[h.name] / weights[h.name], order[h.name]))
		load[best.name] += 1.0
		out[job_name] = best
	return out


def summarise_assignment(assignment: dict[str, HostSpec]) -> dict[str, list[str]]:
	"""Group an assignment into ``{host_name: [job_name, ...]}`` for logging."""
	grouped: dict[str, list[str]] = {}
	for job_name, host in assignment.items():
		grouped.setdefault(host.name, []).append(job_name)
	return grouped


def total_capacity(hosts: list[HostSpec], batch_cpus_per_job: int = 1) -> int:
	"""Sum of every machine's concurrency capacity."""
	return sum(h.capacity(batch_cpus_per_job) for h in hosts)
