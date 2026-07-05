"""JobSpec — typed configuration replacing bare dicts. Validates at construction time."""

from __future__ import annotations
from dataclasses import dataclass, field
import copy


@dataclass
class HookSpec:
	script_path: str
	tasks: list[dict] = field(default_factory=list)


@dataclass
class PreparationSpec:
	kind: str          # 'inp_based' | 'model_generation'
	source_path: str   # base_inp_path or model_script_path
	params: dict = field(default_factory=dict)


@dataclass
class JobSpec:
	"""Single job configuration. Construct-then-validate — errors surface before run_batch."""

	job_name: str
	workflow: str = 'modular'                    # 'modular' | 'monolithic'
	preparation: PreparationSpec | None = None
	monolithic_script: str | None = None
	monolithic_params: dict = field(default_factory=dict)
	pre_extraction: list[HookSpec] = field(default_factory=list)
	post_extraction: list[HookSpec] = field(default_factory=list)

	def __post_init__(self):
		if self.workflow not in ('modular', 'monolithic'):
			raise ValueError(f"[{self.job_name}] unknown workflow: {self.workflow}")
		if self.workflow == 'modular' and self.preparation is None:
			raise ValueError(f"[{self.job_name}] modular workflow requires 'preparation'")
		if self.workflow == 'monolithic' and not self.monolithic_script:
			raise ValueError(f"[{self.job_name}] monolithic workflow requires 'monolithic_script'")

	@classmethod
	def from_dict(cls, d: dict) -> "JobSpec":
		"""Compatibility bridge from old dict format. Deep-copies to avoid shared references."""
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
