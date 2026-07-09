"""JobSpec and related configuration dataclasses — typed, validated at construction.

Replaces the legacy dict-based config format.  :class:`JobSpec` validates itself
in ``__post_init__`` so errors are caught before batch execution begins.
"""

from __future__ import annotations
from dataclasses import dataclass, field
import copy
import warnings


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
class PreparationSpec:
	"""Specification for the preparation phase of a modular workflow.

	Attributes
	----------
	kind : str
		Preparation strategy identifier.  Currently ``'inp_based'`` or
		``'model_generation'``.
	source_path : str
		Path to the base INP file (for ``inp_based``) or model-generation
		script (for ``model_generation``).
	params : dict
		Key-value parameters forwarded to the preparation strategy (e.g.
		placeholder replacements for ``inp_based``).
	options : dict
		Additional options for the preparation strategy (Currently only used by ``existing_inp``):
		- 'staging_mode' (str): ``'copy'`` (default) 
		- 'resolve_includes' (bool): Whether to resolve ``*INCLUDE`` directives in the INP file (default: True).
	"""
	kind: str
	source_path: str
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

	@classmethod
	def from_dict(cls, d: dict) -> "JobSpec":
		"""Migration bridge: construct a :class:`JobSpec` from a legacy dict.

		Deep-copies the input dict so the returned spec owns all of its
		mutable data (no shared references with the caller).

		Parameters
		----------
		d : dict
			Legacy configuration dict.  Recognised keys:
			``job_name``, ``workflow``, ``type``, ``base_inp_path``,
			``model_script_path``, ``script_path``, ``params``,
			``pre_extraction``, ``post_extraction``.

		Returns
		-------
		JobSpec
			Fully validated spec.
		"""
		d = copy.deepcopy(d)
		workflow = d.get('workflow', 'modular')

		prep = None
		if workflow == 'modular':
			prep = PreparationSpec(
				kind=d.get('type', 'inp_based'),
				source_path=d.get('base_inp_path') or d.get('model_script_path') or '',
				params=copy.deepcopy(d.get('params', {})))

		return cls(
			job_name=d['job_name'],
			workflow=workflow,
			preparation=prep,
			monolithic_script=d.get('script_path') if workflow == 'monolithic' else None,
			monolithic_params=copy.deepcopy(d.get('params', {})) if workflow == 'monolithic' else {},
			pre_extraction=[HookSpec(**h) for h in d.get('pre_extraction', [])],
			post_extraction=[HookSpec(**h) for h in d.get('post_extraction', [])],
		)
