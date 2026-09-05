"""Strategy registry — open/closed mapping from preparation kind to factory.

Replaces hardcoded ``if/else`` dispatch.  Users can add custom preparation
strategies at runtime via :func:`register_preparation` without modifying
framework code.
"""

from .spec import HookSpec, JobSpec
from .strategies import (
	DatExtractionStrategy,
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


# ---- Post-extraction strategy factories, keyed by HookSpec.source ----
EXTRACTION_REGISTRY: dict[str, callable] = {
	# Each factory receives a list[HookSpec] and returns an ExtractionStrategy.
	# Extend via register_extraction() to teach ABQflow a new artifact (.msg,
	# .fil, ...) without touching build_workflow.
	'odb': OdbExtractionStrategy,
	'dat': DatExtractionStrategy,
}


def register_extraction(source: str, factory: callable):
	"""Register a post-extraction strategy for a :attr:`HookSpec.source` value.

	Parameters
	----------
	source : str
		Value users will put in ``HookSpec(source=...)``.  Add it to
		:data:`~ABQflow.core.spec.HOOK_SOURCES` as well, or ``HookSpec``
		rejects it before :func:`build_workflow` is ever reached.
	factory : callable
		Callable that receives a ``list[HookSpec]`` and returns an
		:class:`~ABQflow.core.strategies.ExtractionStrategy`.
	"""
	EXTRACTION_REGISTRY[source] = factory


def _group_by_source(hooks: list[HookSpec]) -> list[tuple[str, list[HookSpec]]]:
	"""Split *hooks* into consecutive runs that share a ``source``.

	Runs, not buckets: bucketing ``[odb, dat, odb]`` would quietly reorder the
	user's hooks, and hook order is observable — a later hook may overwrite an
	earlier result name.  A uniform list still collapses to a single group, so
	the all-ODB specs that existed before ``source`` build exactly the strategy
	chain they always did.
	"""
	groups: list[tuple[str, list[HookSpec]]] = []
	for hook in hooks:
		if groups and groups[-1][0] == hook.source:
			groups[-1][1].append(hook)
		else:
			groups.append((hook.source, [hook]))
	return groups


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

	Post-extraction hooks are grouped into consecutive runs sharing a
	:attr:`~ABQflow.core.spec.HookSpec.source` and each run is handed to the
	matching factory in :data:`EXTRACTION_REGISTRY`, so declaration order
	survives a list that mixes ``'odb'`` and ``'dat'`` hooks.

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
		If ``spec.preparation.kind`` or a hook's ``source`` is not registered.
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

	post = []
	for source, hooks in _group_by_source(spec.post_extraction):
		try:
			factory = EXTRACTION_REGISTRY[source]
		except KeyError:
			raise ValueError(
				f"Unknown extraction source: '{source}'. "
				f"Available: {list(EXTRACTION_REGISTRY)}"
			) from None
		post.append(factory(hooks))

	compile_strategy = SubroutineCompileStrategy(spec.subroutine) if spec.subroutine else None

	return ModularWorkflowStrategy(prep, pre, post, preflight_mode=spec.preflight,
	                                preflight_only=preflight_only,
	                                compile_strategy=compile_strategy)
