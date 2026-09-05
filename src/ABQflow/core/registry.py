"""Strategy registry — open/closed mapping from preparation kind to factory.

Replaces hardcoded ``if/else`` dispatch.  Users can add custom preparation
strategies at runtime via :func:`register_preparation` without modifying
framework code.
"""

from .spec import JobSpec, PreparationSpec
from .strategies import (
	ExistingInpStrategy,
	InpModifyStrategy,
	JobWorkflowStrategy,
	ModelGenerationStrategy,
	ModelPropertiesExtractionStrategy,
	ModularWorkflowStrategy,
	MonolithicWorkflowStrategy,
	OdbExtractionStrategy,
	SubroutineCompileStrategy,
)

# Option keys the INP-based kinds understand.  Shared by both because they are
# two presets over one strategy.
_INP_OPTIONS = frozenset({'resolve_includes', 'include_staging'})


def _checked_options(spec: PreparationSpec, allowed: frozenset) -> dict:
	"""Reject option keys the strategy would otherwise ignore in silence.

	``PreparationSpec.options`` is an untyped dict, so a key that belongs to
	another kind — or a typo — used to be dropped without a word, and the run
	proceeded with a setting the user believed was in effect.  Failing here
	costs one clear exception instead of a silently mis-configured batch.
	"""
	unknown = set(spec.options) - allowed
	if unknown:
		raise ValueError(
			f"Unknown preparation option(s) for kind='{spec.kind}': {sorted(unknown)}. "
			f"Known options: {sorted(allowed)}.")
	return spec.options


# ---- Preparation strategy factories ----
PREPARATION_REGISTRY: dict[str, callable] = {
	# Each factory receives a PreparationSpec and returns a PreparationStrategy.
	# Add new entries via register_preparation() to keep the framework closed
	# for modification but open for extension.
	#
	# 'inp_based' and 'existing_inp' are two presets over one implementation
	# (InpPreparationStrategy): the second is the first with an empty parameter
	# set plus an assertion that none was needed.  Both names are kept because
	# they state different intents at the call site.
	'inp_based':        lambda s: InpModifyStrategy(
		s.source_path,
		s.params,
		resolve_includes=_checked_options(s, _INP_OPTIONS).get('resolve_includes', True),
		include_staging=s.options.get('include_staging', 'reference'),
	),
	'model_generation': lambda s: ModelGenerationStrategy(s.source_path, s.params),
	'existing_inp':     lambda s: ExistingInpStrategy(
		s.source_path,
		resolve_includes=_checked_options(s, _INP_OPTIONS).get('resolve_includes', True),
		include_staging=s.options.get('include_staging', 'reference'),
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


def build_workflow(spec: JobSpec, preflight_only: bool = False) -> JobWorkflowStrategy:
	"""Assemble a concrete :class:`~abaqus_batch_pack.strategies.JobWorkflowStrategy` from a spec.

	* Monolithic specs produce a :class:`~abaqus_batch_pack.strategies.MonolithicWorkflowStrategy`.
	* Modular specs look up the preparation kind in :data:`PREPARATION_REGISTRY`,
	  wrap pre/post-extraction hooks, and return a
	  :class:`~abaqus_batch_pack.strategies.ModularWorkflowStrategy`.

	Parameters
	----------
	spec : JobSpec
		Validated job configuration.
	preflight_only : bool
		If ``True``, the workflow stops after preflight (IMP-04).

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

	pre = [ModelPropertiesExtractionStrategy(spec.pre_extraction)] if spec.pre_extraction else []
	post = [OdbExtractionStrategy(spec.post_extraction)] if spec.post_extraction else []

	compile_strategy = SubroutineCompileStrategy(spec.subroutine) if spec.subroutine else None

	return ModularWorkflowStrategy(prep, pre, post, preflight_mode=spec.preflight,
	                                preflight_only=preflight_only,
	                                compile_strategy=compile_strategy)
