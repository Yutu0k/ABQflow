"""Resolution of an INP file's ``*INCLUDE`` tree — shared by every INP-based preparation.

Why this is one module rather than logic inside each strategy
-------------------------------------------------------------
``InpModifyStrategy`` and ``ExistingInpStrategy`` were solving two halves of
the same problem and neither half was complete: the first substituted
``{{placeholder}}`` tokens but left ``*INCLUDE`` paths alone (so a template
written into the job directory pointed its relative includes at the *job*
directory, where they do not exist), the second rewrote includes but rejected
templates outright.  A deck that is both parameterised *and* modular — the
common shape for a scenario file that includes a shared mesh and a material
fragment — could not be expressed at all.

Resolving the whole tree in one place makes ``existing_inp`` simply the
``params={}`` case of ``inp_based``, and gives the two strategies the same
behaviour on nested includes for free.

The two tiers
-------------
Every file in the tree is classified once:

**parameterised** — it contains ``{{...}}`` of its own, *or* one of its
descendants does.  The descendant clause matters: if a static fragment
includes a parameterised one, the fragment's own ``*INCLUDE`` line has to be
rewritten to point at the per-job copy, so the fragment cannot be shared
either.  Parameterised files are substituted and written into the job
directory, and the directive naming them becomes a **bare filename** (Abaqus
resolves those against the job's working directory).

**static** — everything else, typically the large mesh or geometry that is
identical across the batch.  The file is left alone on disk and the directive
naming it becomes its **local absolute path**, so the deck resolves from any
working directory.

That split is deliberately readable back off the deck: after resolution a
bare filename *means* "per-job file, sitting in the job directory" and an
absolute path *means* "shared file that N jobs point at".  Remote staging can
therefore decide where each target belongs by looking at the deck alone,
without a manifest threaded through from preparation.
"""

from __future__ import annotations

import codecs
import hashlib
import logging
import os
import re
from dataclasses import dataclass

# Canonical shape of the directive.  Abaqus accepts INPUT= with or without
# quotes and the keyword is case-insensitive; anchoring to the start of a line
# keeps the pattern from matching the word inside a comment.  Every module in
# the package uses this one object so the copies cannot drift apart again.
INCLUDE_RE = re.compile(
	r'(^[ \t]*\*INCLUDE\s*,\s*INPUT\s*=\s*)(["\']?)([^"\'\r\n]+)\2',
	re.IGNORECASE | re.MULTILINE,
)

# ``{{name}}`` substitution markers (see InpModifyStrategy).
PLACEHOLDER_RE = re.compile(r'\{\{(\w+)\}\}')

# Presence check for a solve step.  Scanned across the whole tree, not just
# the root: putting *STEP in an included fragment is normal practice, and the
# root-only check rejected such decks as "not a valid Abaqus input file".
_STEP_RE = re.compile(r'^[ \t]*\*STEP', re.IGNORECASE | re.MULTILINE)


class IncludeResolutionError(Exception):
	"""An ``*INCLUDE`` tree could not be resolved.

	Raised for a target that does not exist on disk, a file that cannot be
	read, or a cycle.  Carries the referencing file and the path the
	directive resolved to, because "file not found" without both of those is
	nearly useless when includes nest.
	"""


def read_inp_text(path: str) -> str:
	"""Read an INP file without depending on the machine's locale.

	Python's text mode defaults to the system encoding — GBK on a Chinese
	Windows, cp1252 elsewhere — so a plain ``open(path)`` raises on any deck
	those codecs reject.  A UTF-8 byte-order mark is enough to trigger it,
	which makes decks exported by many editors unreadable on exactly the
	machines this package targets.

	Decoding order: UTF-8 (BOM stripped if present), then latin-1, which
	never fails and preserves the bytes.  Abaqus keywords are ASCII either
	way, so the fallback only affects comments and free-text fields.
	"""
	with open(path, 'rb') as f:
		raw = f.read()
	if raw.startswith(codecs.BOM_UTF8):
		raw = raw[len(codecs.BOM_UTF8):]
	try:
		return raw.decode('utf-8')
	except UnicodeDecodeError:
		return raw.decode('latin-1')


def write_inp_text(path: str, content: str) -> None:
	"""Write INP text as UTF-8, leaving line endings exactly as given."""
	with open(path, 'w', encoding='utf-8', newline='') as f:
		f.write(content)


@dataclass(frozen=True)
class ResolvedInp:
	"""Outcome of walking one ``*INCLUDE`` tree — text only, nothing written.

	Keeping the writes in the caller means a tree that fails validation (an
	uncovered placeholder, a missing ``*STEP``) leaves no half-built job
	directory behind.

	Attributes
	----------
	root_text : str
		The root deck, includes rewritten and placeholders substituted, ready
		to be written to ``ctx.inp_path``.
	materialized : dict[str, str]
		``{filename: content}`` for every parameterised include.  Each must be
		written into the job directory under exactly that filename — the
		rewritten directives name them without a path.
	shared : tuple[str, ...]
		Absolute local paths of the static includes the tree points at, in
		first-seen order.  Informational here; remote staging re-derives the
		same set from the deck.
	placeholders : frozenset[str]
		Every ``{{name}}`` seen anywhere in the tree, so the caller can check
		parameter coverage across all files rather than only the root.
	has_step : bool
		Whether any file in the tree contains a ``*STEP`` keyword.
	"""

	root_text: str
	materialized: dict[str, str]
	shared: tuple[str, ...]
	placeholders: frozenset[str]
	has_step: bool


@dataclass
class _Node:
	"""One resolved file: whether it is per-job, and its rewritten text."""

	parameterized: bool
	text: str


def resolve_target(raw: str, base_dir: str) -> str:
	"""Resolve one raw ``INPUT=`` value against the file that referenced it.

	Relative targets are resolved against the *referencing file's* directory,
	which is what Abaqus does and what makes a directory of scenario decks
	portable.  Separators are normalised both ways: decks are routinely
	authored on one platform and run on another, and Abaqus accepts either.
	"""
	path = raw.strip().replace('\\', os.sep).replace('/', os.sep)
	if os.path.isabs(path):
		return os.path.normpath(path)
	return os.path.normpath(os.path.join(base_dir, path))


def resolve_include_tree(
	root_path: str,
	params: dict | None = None,
	*,
	reserved_names: tuple[str, ...] = (),
	follow_includes: bool = True,
	logger: logging.Logger | None = None,
) -> ResolvedInp:
	"""Walk *root_path*'s include tree, substituting params and rewriting paths.

	Parameters
	----------
	root_path : str
		The deck to resolve.  Always treated as per-job: its text comes back
		in :attr:`ResolvedInp.root_text` rather than in ``materialized``.
	params : dict, optional
		``{{placeholder}}`` substitutions, applied to every file in the tree.
		A placeholder with no matching key is left **untouched** rather than
		raising, so the caller can report the full set of missing parameters
		at once instead of failing on the first one.
	reserved_names : tuple[str, ...]
		Filenames already spoken for in the job directory (at minimum the job's
		own ``<job_name>.inp``).  A parameterised include whose basename
		collides with one of these — or with another include already placed —
		is renamed with a short digest of its source path rather than
		overwriting it.
	follow_includes : bool
		If ``False``, the root is substituted but its ``*INCLUDE`` lines are
		left exactly as written.  Escape hatch for decks whose includes are
		resolved by something outside ABQflow.
	logger : logging.Logger, optional
		Progress destination; a module logger is used if omitted.

	Returns
	-------
	ResolvedInp
		Text to write, plus what the caller needs to validate it.

	Raises
	------
	IncludeResolutionError
		A target is missing or unreadable, or the tree contains a cycle.
	"""
	log = logger or logging.getLogger(__name__)
	params = params or {}

	root_abs = os.path.abspath(root_path)

	placeholders: set[str] = set()
	shared: list[str] = []
	shared_seen: set[str] = set()
	materialized: dict[str, str] = {}

	# Per-job filenames, memoised by normcased source path so a file included
	# from two places is written once and both directives agree on the name.
	names: dict[str, str] = {}
	claimed: set[str] = {n.lower() for n in reserved_names}

	cache: dict[str, _Node] = {}
	has_step = False

	def _local_name(abs_path: str) -> str:
		key = os.path.normcase(abs_path)
		if key in names:
			return names[key]
		base = os.path.basename(abs_path)
		stem, ext = os.path.splitext(base)
		candidate = base
		if candidate.lower() in claimed:
			# Two different sources sharing a basename, or a clash with the
			# job's own INP.  Digest the source path so the choice is stable
			# across runs and across machines.
			digest = hashlib.sha256(key.encode('utf-8')).hexdigest()[:8]
			candidate = f'{stem}_{digest}{ext}'
		claimed.add(candidate.lower())
		names[key] = candidate
		return candidate

	def _visit(abs_path: str, stack: tuple[str, ...]) -> _Node:
		nonlocal has_step

		key = os.path.normcase(abs_path)
		if key in cache:
			return cache[key]
		if key in {os.path.normcase(p) for p in stack}:
			chain = ' -> '.join(stack + (abs_path,))
			raise IncludeResolutionError(f'*INCLUDE cycle detected: {chain}')

		try:
			text = read_inp_text(abs_path)
		except OSError as e:
			raise IncludeResolutionError(f"cannot read INP '{abs_path}': {e}") from None

		if _STEP_RE.search(text):
			has_step = True

		own = set(PLACEHOLDER_RE.findall(text))
		placeholders.update(own)

		# Resolve children first: whether this file is per-job depends on them.
		targets: dict[str, str] = {}
		children: dict[str, _Node] = {}
		if follow_includes:
			base_dir = os.path.dirname(abs_path)
			for m in INCLUDE_RE.finditer(text):
				raw = m.group(3).strip()
				if raw in targets:
					continue
				target = resolve_target(raw, base_dir)
				if not os.path.isfile(target):
					raise IncludeResolutionError(
						f"*INCLUDE target not found: '{raw}' referenced from "
						f"'{abs_path}' resolved to '{target}'")
				targets[raw] = target
				children[raw] = _visit(target, stack + (abs_path,))

		parameterized = bool(own) or any(c.parameterized for c in children.values())

		if targets:
			def _rewrite(m: re.Match) -> str:
				raw = m.group(3).strip()
				target = targets[raw]
				if children[raw].parameterized:
					local = _local_name(target)
					log.info('  INCLUDE (per-job): %s -> %s', raw, local)
					return m.group(1) + local
				if target not in shared_seen:
					shared_seen.add(target)
					shared.append(target)
					log.info('  INCLUDE (shared): %s -> %s', raw, target)
				return m.group(1) + target

			text = INCLUDE_RE.sub(_rewrite, text)

		if own:
			text = PLACEHOLDER_RE.sub(
				lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0),
				text)

		node = _Node(parameterized=parameterized, text=text)
		cache[key] = node
		if parameterized and key != os.path.normcase(root_abs):
			materialized[_local_name(abs_path)] = text
		return node

	root = _visit(root_abs, ())

	return ResolvedInp(
		root_text=root.text,
		materialized=dict(materialized),
		shared=tuple(shared),
		placeholders=frozenset(placeholders),
		has_step=has_step,
	)
