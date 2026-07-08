"""Strategy registry — open/closed mapping from preparation kind to factory.

Replaces hardcoded ``if/else`` dispatch.  Users can add custom preparation
strategies at runtime via :func:`register_preparation` without modifying
framework code.
"""

from .spec import JobSpec
from .strategies import (
	JobWorkflowStrategy, ModularWorkflowStrategy, MonolithicWorkflowStrategy,
	InpModifyStrategy, ModelGenerationStrategy, ExistingInpStrategy,
	OdbExtractionStrategy, ModelPropertiesExtractionStrategy,
)

# ---- Preparation strategy factories ----
PREPARATION_REGISTRY: dict[str, callable] = {
	# Each factory receives a PreparationSpec and returns a PreparationStrategy.
	# Add new entries via register_preparation() to keep the framework closed
	# for modification but open for extension.
	'inp_based':        lambda s: InpModifyStrategy(s.source_path, s.params),
	'model_generation': lambda s: ModelGenerationStrategy(s.source_path, s.params),
	'existing_inp':     lambda s: ExistingInpStrategy(
		s.source_path,
		staging_mode=s.options.get('staging_mode', 'copy'),
		resolve_includes=s.options.get('resolve_includes', True),
	),
}


def register_preparation(kind: str, factory: callable):
	"""Register a custom preparation strategy for use in modular workflows.

	After registration, users can set ``PreparationSpec.kind`` to *kind* and
	:func:`build_workflow` will dispatch to *factory* automatically — no
	framework source changes required.

	Parameters
	----------
	kind : str
		Unique key for the preparation strategy (referenced in
		:class:`PreparationSpec.kind <abaqus_batch_pack.spec.PreparationSpec>`).
	factory : callable
		Callable that receives a :class:`~abaqus_batch_pack.spec.PreparationSpec`
		and returns a :class:`~abaqus_batch_pack.strategies.PreparationStrategy`.
	"""
	PREPARATION_REGISTRY[kind] = factory


def build_workflow(spec: JobSpec) -> JobWorkflowStrategy:
	"""Assemble a concrete :class:`~abaqus_batch_pack.strategies.JobWorkflowStrategy` from a spec.

	* Monolithic specs produce a :class:`~abaqus_batch_pack.strategies.MonolithicWorkflowStrategy`.
	* Modular specs look up the preparation kind in :data:`PREPARATION_REGISTRY`,
	  wrap pre/post-extraction hooks, and return a
	  :class:`~abaqus_batch_pack.strategies.ModularWorkflowStrategy`.

	Parameters
	----------
	spec : JobSpec
		Validated job configuration.

	Returns
	-------
	JobWorkflowStrategy
		Ready-to-execute strategy chain.

	Raises
	------
	ValueError
		If ``spec.preparation.kind`` is not registered.
	"""
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
