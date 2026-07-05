"""Strategy registry — replaces hardcoded if/else, fixes open/closed principle violation."""

from .spec import JobSpec
from .strategies import (
	JobWorkflowStrategy, ModularWorkflowStrategy, MonolithicWorkflowStrategy,
	InpModifyStrategy, ModelGenerationStrategy,
	OdbExtractionStrategy, ModelPropertiesExtractionStrategy,
)

# ---- Preparation strategy factories ----
PREPARATION_REGISTRY: dict[str, callable] = {
	'inp_based':        lambda s: InpModifyStrategy(s.source_path, s.params),
	'model_generation': lambda s: ModelGenerationStrategy(s.source_path, s.params),
}


def register_preparation(kind: str, factory: callable):
	"""Users can register custom preparation strategies without modifying framework code."""
	PREPARATION_REGISTRY[kind] = factory


def build_workflow(spec: JobSpec) -> JobWorkflowStrategy:
	"""Build the workflow strategy chain from a JobSpec."""
	if spec.workflow == 'monolithic':
		return MonolithicWorkflowStrategy(spec.monolithic_script,
										spec.monolithic_params)

	try:
		prep = PREPARATION_REGISTRY[spec.preparation.kind](spec.preparation)
	except KeyError:
		raise ValueError(
			f"Unknown preparation kind: '{spec.preparation.kind}'. "
			f"Available: {list(PREPARATION_REGISTRY)}"
		) from None

	pre = [ModelPropertiesExtractionStrategy(
		[{'script_path': h.script_path, 'tasks': h.tasks} for h in spec.pre_extraction]
	)] if spec.pre_extraction else []

	post = [OdbExtractionStrategy(
		[{'script_path': h.script_path, 'tasks': h.tasks} for h in spec.post_extraction]
	)] if spec.post_extraction else []

	return ModularWorkflowStrategy(prep, pre, post)
