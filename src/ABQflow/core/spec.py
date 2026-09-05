"""JobSpec and related configuration dataclasses — typed, validated at construction.

Replaces the legacy dict-based config format.  :class:`JobSpec` validates itself
in ``__post_init__`` so errors are caught before batch execution begins.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field


@dataclass
class HookSpec:
	"""Description of one extraction/pre-extraction hook script and its tasks.

	Attributes
	----------
	script_path : str
		Path to the Python script that processes the hook.
	tasks : list[dict]
		List of task descriptors; each dict typically contains
		``result_name``, ``script_path``, and task-specific parameters.
	"""
	script_path: str
	tasks: list[dict] = field(default_factory=list)


@dataclass
class SubroutineSpec:
	"""Specification for an Abaqus user subroutine (UMAT/VUMAT/UEL/...).

	Attributes
	----------
	source_path : str
		Path to the subroutine source file (or, when ``precompiled=True``,
		to the already-compiled object/library).
	language : str
		``'fortran'`` (default), ``'c'``, or ``'cpp'``.
	solver : str
		Target solver: ``'standard'`` (default), ``'explicit'``, or ``'cfd'``.
		Controls the flag passed to ``abaqus make`` (see
		:meth:`~ABQflow.core.runner.AbaqusRunner.build_make_command`).
	precompiled : bool
		If ``True``, skip the compile phase entirely and pass
		``source_path`` straight through to ``user=`` on the solver/preflight
		commands. Default ``False``.
	"""
	source_path: str
	language: str = 'fortran'
	solver: str = 'standard'
	precompiled: bool = False

	def __post_init__(self):
		if self.language not in ('fortran', 'c', 'cpp'):
			raise ValueError(
				f"SubroutineSpec.language must be 'fortran', 'c', or 'cpp'; got '{self.language}'.")
		if self.solver not in ('standard', 'explicit', 'cfd'):
			raise ValueError(
				f"SubroutineSpec.solver must be 'standard', 'explicit', or 'cfd'; got '{self.solver}'.")


@dataclass
class PreparationSpec:
	"""Specification for the preparation phase of a modular workflow.

	Attributes
	----------
	kind : str
		Preparation strategy identifier: ``'inp_based'``, ``'existing_inp'``,
		or ``'model_generation'``.

		The first two are one implementation
		(:class:`~ABQflow.core.strategies.InpPreparationStrategy`) under two
		names, because "fill in a template" and "use a finished deck" are the
		same pipeline with a different amount of work in its first step.  What
		separates them is **what varies across the batch**, and therefore which
		generator you pair the spec with:

		- ``'inp_based'`` — one template, N parameter sets.  Pair with
		  :func:`~ABQflow.helpers.convert.generate_from_array`, or set
		  ``params`` yourself.
		- ``'existing_inp'`` — N already-written decks.  Pair with
		  :func:`~ABQflow.helpers.convert.generate_from_inp_files`, which fills
		  ``kind`` and ``source_path`` in for you.  Uses no ``params``, and
		  reports a leftover ``{{placeholder}}`` as "you handed a template to a
		  batch of finished decks" rather than as a missing parameter.

	source_path : str
		Path to the template or finished INP (for ``inp_based`` /
		``existing_inp``) or the model-generation script (for
		``model_generation``).

		**Optional when a generator supplies it.**
		:func:`~ABQflow.helpers.convert.generate_from_inp_files` fills it in
		per file, so a base spec headed for that generator should leave it
		empty rather than naming an unrelated INP — the same way a base spec
		headed for :func:`~ABQflow.helpers.convert.generate_from_array` leaves
		``params`` empty.  Setting it anyway is not silently accepted: the
		generator warns that your value is being discarded.  A spec that
		reaches preparation with it still empty fails with a message naming
		both ways to fix it.
	params : dict
		Key-value parameters forwarded to the preparation strategy:
		``{{placeholder}}`` replacements for ``inp_based``, CLI arguments for
		``model_generation``.  Not used by ``existing_inp``.  Left empty when
		:func:`~ABQflow.helpers.convert.generate_from_array` supplies it.
	options : dict
		Additional options.  **Unknown keys raise** rather than being ignored,
		so a typo or an option borrowed from another kind fails loudly.

		- ``'resolve_includes'`` (bool, default ``True``): whether to walk and
		  rewrite the ``*INCLUDE`` tree.  Understood by ``inp_based`` and
		  ``existing_inp``; see :mod:`ABQflow.core.inp_include` for what the
		  walk does with parameterized versus static includes.
		- ``'include_staging'`` (str, default ``'reference'``): what to do with
		  the *static* includes.  ``'reference'`` leaves them in place and
		  points the deck at their absolute paths, so a shared mesh is never
		  copied.  Parameterized includes have no such choice — their content
		  exists nowhere on disk, so they are always written into the job
		  directory.
	"""
	kind: str
	source_path: str = ''
	params: dict = field(default_factory=dict)
	options: dict = field(default_factory=dict)


@dataclass
class JobSpec:
	"""Single-job configuration validated at construction time.

	Fails fast — validation runs in ``__post_init__`` so invalid configs are
	rejected before any Abaqus process is launched.

	Attributes
	----------
	job_name : str
		Unique name for this job (also used as the working directory name).
	workflow : str
		``'modular'`` (default, 4-phase pipeline) or ``'monolithic'``
		(single-script).
	preparation : PreparationSpec or None
		Preparation spec; required when ``workflow='modular'``, ignored for
		monolithic.
	preflight : str, default=None
		Preflight mode for modular workflows. 
		- None: No preflight checks (default)
		- 'syntaxcheck': Run abaqus syntax check
		- 'datacheck': Run abaqus datacheck
	monolithic_script : str or None
		Path to the monolithic script; required when
		``workflow='monolithic'``.
	monolithic_params : dict
		Parameters forwarded to the monolithic script as ``--key value`` args.
	pre_extraction : list[HookSpec]
		Hooks run *before* the solver (e.g. model property extraction).
	post_extraction : list[HookSpec]
		Hooks run *after* the solver (e.g. ODB result extraction).
	subroutine : SubroutineSpec or None
		User subroutine to compile and pass via ``user=`` to the solver
		(modular workflow only; ignored for ``workflow='monolithic'``).
	meta : dict
		Arbitrary user metadata
	"""

	job_name: str
	workflow: str = 'modular'
	preparation: PreparationSpec | None = None
	preflight: str | None = None  # IMP-04: None | 'syntaxcheck' | 'datacheck'
	monolithic_script: str | None = None
	monolithic_params: dict = field(default_factory=dict)
	pre_extraction: list[HookSpec] = field(default_factory=list)
	post_extraction: list[HookSpec] = field(default_factory=list)
	subroutine: SubroutineSpec | None = None
	meta: dict = field(default_factory=dict)

	def __post_init__(self):
		"""Validate the spec after field assignment.

		Validation rules:

		* ``workflow`` must be ``'modular'`` or ``'monolithic'``.
		* Modular workflow requires a non-``None`` ``preparation``.
		* Monolithic workflow requires a non-empty ``monolithic_script``.

		Raises
		------
		ValueError
			If any validation rule is violated.
		"""
		if self.workflow not in ('modular', 'monolithic'):
			raise ValueError(f"[{self.job_name}] unknown workflow: {self.workflow}")
		if self.workflow == 'modular' and self.preparation is None:
			raise ValueError(f"[{self.job_name}] modular workflow requires 'preparation'")
		if self.workflow == 'monolithic' and not self.monolithic_script:
			raise ValueError(f"[{self.job_name}] monolithic workflow requires 'monolithic_script'")
		if self.preflight is not None and self.preflight not in ('syntaxcheck', 'datacheck'):
			raise ValueError(
				f"[{self.job_name}] preflight must be 'syntaxcheck', 'datacheck', or None; "
				f"got '{self.preflight}'."
			)
		if (self.preparation is not None
				and self.preparation.kind == 'existing_inp'
				and self.preparation.params):
			warnings.warn(
				f"[{self.job_name}] kind='existing_inp' does not use params — "
				f"params will be ignored. If you need template substitution, use kind='inp_based'."
			)
