"""Job workflow strategies — the ABC hierarchy and all concrete implementations.

Strategies are stateless (configuration only in ``__init__``) and depend on
three injected arguments at call time: :class:`~abaqus_batch_pack.context.JobContext`,
:class:`~abaqus_batch_pack.runner.AbaqusRunner`, and ``logging.Logger``.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import asdict
from typing import List

from .context import JobContext
from .diagnostics import SolverResult
from .inp_include import (
	IncludeResolutionError,
	ResolvedInp,
	resolve_include_tree,
	write_inp_text,
)
from .runner import AbaqusRunner, extract_json
from .spec import HookSpec, SubroutineSpec
from .status import JobStatus, JobStatusManager


def _stage_resolved(resolution: ResolvedInp, ctx: JobContext,
					logger: logging.Logger) -> bool:
	"""Write a resolved tree into the job directory: includes first, then the root.

	Order matters only for legibility of a failed run — if writing an include
	fails, the root deck that references it was never created, so nothing
	downstream can pick up a half-assembled job.
	"""
	try:
		for filename, content in resolution.materialized.items():
			write_inp_text(os.path.join(ctx.output_dir, filename), content)
			logger.info(f"  Materialized parameterized include: {filename}")
		write_inp_text(ctx.inp_path, resolution.root_text)
	except OSError as e:
		logger.error(f"Failed to write INP into job directory: {e}")
		return False
	logger.info(f"Successfully created INP file: {ctx.inp_path}")
	return True


# ======================== Preparation Strategies ========================
class PreparationStrategy(ABC):
	"""Interface for preparation: produce an INP file at ``ctx.inp_path``.

	Subclasses
	----------
	InpPreparationStrategy
		Template-based INP generation (``{{placeholder}}`` substitution).
	ModelGenerationStrategy
		Run an external script that produces the INP (requires CAE kernel).
	"""

	@abstractmethod
	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		"""Produce the INP file.

		Parameters
		----------
		ctx : JobContext
			Job context providing ``inp_path`` and ``output_dir``.
		runner : AbaqusRunner
			Subprocess runner (may not be used by every strategy).
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		bool
			``True`` if the INP was produced, ``False`` otherwise.
		"""
		...


class InpPreparationStrategy(PreparationStrategy):
	"""Turn an INP file — template or finished deck — into a runnable job directory.

	One pipeline serves both use cases, because they are the same pipeline with
	a different amount of work in the first step::

		substitute  ->  validate  ->  stage

	*substitute* walks the ``*INCLUDE`` tree once, replacing ``{{placeholder}}``
	tokens and rewriting every directive (see :mod:`ABQflow.core.inp_include`
	for the per-file rules).  With ``params`` empty there is simply nothing to
	substitute, which is exactly the "pre-existing INP" case — it is not a
	separate strategy, just this one with an empty parameter set.

	*validate* checks the resolved tree, never the root alone: a modular deck
	legitimately keeps its ``*STEP`` — or a ``{{placeholder}}`` — in an included
	fragment, so a root-only check gives the wrong answer on exactly the decks
	this class exists to support.

	*stage* writes the result into the job directory.  Nothing is written until
	validation passes, so a rejected deck leaves no half-built job behind.

	Attributes
	----------
	source_path : str
		Template or finished INP to prepare.
	params : dict
		``{{placeholder}}`` substitutions, applied across the whole tree.  Empty
		or ``None`` means "this deck is already finished".
	resolve_includes : bool
		If ``True`` (default), walk and rewrite the ``*INCLUDE`` tree.  Set to
		``False`` to leave the directives exactly as authored.
	assert_finished : bool
		If ``True``, a leftover placeholder is reported as "this is a template,
		you meant to pass params" rather than as a missing parameter.  Set by
		the ``existing_inp`` preset, whose whole point is that assertion.
	include_staging : str
		What to do with the *static* includes — the ones no parameter touches.
		``'reference'`` (default and, today, the only implemented value) leaves
		them where they are and points the deck at their absolute paths, so a
		shared mesh is never copied.  See :meth:`_check_include_staging` for
		why this axis is not the same thing as the old ``staging_mode``.
	"""

	#: Values ``include_staging`` may take today, and what each would mean.
	# TODO
	_STAGING_PLANNED = {
		'reference': 'leave static includes in place, reference them by absolute path',
		'copy': 'copy static includes into the job directory (not implemented yet)',
	}

	def __init__(
		self,
		source_path: str,
		params: dict | None = None,
		*,
		resolve_includes: bool = True,
		assert_finished: bool = False,
		include_staging: str = 'reference',
	):
		self.source_path = source_path
		self.params = params or {}
		self.resolve_includes = resolve_includes
		self.assert_finished = assert_finished
		self.include_staging = include_staging

	def _check_include_staging(self, logger: logging.Logger) -> bool:
		"""Reject an ``include_staging`` value that is named but not built yet.

		This axis replaces the old ``staging_mode``, which asked whether the
		*job's deck* should be copied, linked or run in place.  That question
		has no single answer any more: a parameterised file's content exists
		nowhere on disk, so it *must* be written into the job directory, while
		a static include is already "in place" and merely gets pointed at.  The
		only remaining choice is what happens to the static ones.
		"""
		if self.include_staging == 'reference':
			return True
		if self.include_staging in self._STAGING_PLANNED:
			logger.error(
				f"include_staging='{self.include_staging}' is planned but not implemented "
				f"({self._STAGING_PLANNED[self.include_staging]}). Use 'reference'.")
		else:
			logger.error(
				f"Unknown include_staging: '{self.include_staging}'. "
				f"Known values: {sorted(self._STAGING_PLANNED)}.")
		return False

	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		kind = 'ExistingInp' if self.assert_finished else 'InpModify'
		logger.info(f"Sub strategy [{kind}]: Based on INP file '{self.source_path}'")

		if not self.source_path:
			# Left empty for a generator that never ran, or simply forgotten.
			# Naming both exits beats "file '' not found".
			logger.error(
				"PreparationSpec.source_path is empty. Either set it, or build the "
				"specs with generate_from_inp_files(), which fills it in per file.")
			return False
		if not os.path.isfile(self.source_path):
			logger.error(f"Source INP not found: {self.source_path}")
			return False
		if not self._check_include_staging(logger):
			return False

		# ---- substitute ----
		try:
			resolution = resolve_include_tree(
				self.source_path,
				self.params,
				reserved_names=(os.path.basename(ctx.inp_path),),
				follow_includes=self.resolve_includes,
				logger=logger,
			)
		except IncludeResolutionError as e:
			logger.error(f"Failed to resolve INCLUDE tree: {e}")
			return False
		except Exception as e:
			logger.error(f"Failed to read source INP: {e}")
			return False

		# ---- validate ----
		if not self._validate(resolution, logger):
			return False

		# ---- stage ----
		return _stage_resolved(resolution, ctx, logger)

	def _validate(self, resolution: ResolvedInp, logger: logging.Logger) -> bool:
		"""Check the resolved tree; log the reason and return ``False`` on failure."""
		# A deck with no *STEP anywhere cannot solve. Checked for templates too:
		# it turns an opaque solver error into a clear preparation error, and a
		# template that produces an unsolvable deck is broken either way.
		if not resolution.has_step:
			logger.error("INP contains no *STEP — not a valid Abaqus input file")
			return False

		# B8: placeholder coverage, across the tree rather than the root.
		found = set(resolution.placeholders)
		given = set(map(str, self.params.keys()))
		if missing := found - given:
			if self.assert_finished:
				logger.error(
					"INP contains {{placeholder}} markers — this looks like a template, "
					"not a finished INP. Use kind='inp_based' with params instead. "
					f"Unresolved: {sorted(missing)}")
			else:
				logger.error(f"INP placeholders missing parameters: {missing}")
			return False
		if unused := given - found:
			logger.warning(f"Parameters not used in INP: {unused}")
		return True


class InpModifyStrategy(InpPreparationStrategy):
	"""Preset of :class:`InpPreparationStrategy` that substitutes parameters.

	Backs ``kind='inp_based'``.  Kept as its own name because the two presets
	state different intents at the call site — this one says "this deck is a
	template".

	Attributes
	----------
	source_path : str
		Template INP containing ``{{key}}`` placeholders.
	params : dict
		Mapping of placeholder names to substitution values.
	"""

	def __init__(
		self,
		source_path: str,
		params: dict,
		resolve_includes: bool = True,
		include_staging: str = 'reference'
	):
		super().__init__(source_path, params,
						resolve_includes=resolve_includes,
						assert_finished=False,
						include_staging=include_staging)


class ModelGenerationStrategy(PreparationStrategy):
	"""Run a model-generation script (requires CAE kernel / ``mdb`` access).

	The script is launched via ``abaqus cae noGUI=<script>`` and is expected
	to produce an INP file at ``ctx.inp_path``.  Common arguments
	(``--job_name``, user params) are forwarded as CLI flags.

	Attributes
	----------
	model_script_path : str
		Path to the model-generation script.
	script_params : dict
		Key-value pairs forwarded as ``--key value`` arguments.
	"""

	def __init__(self, model_script_path: str, script_params: dict):
		self.model_script_path = model_script_path
		self.script_params = script_params

	def prepare(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> bool:
		logger.info(f"Sub Strategy [ModelGeneration]: Run script '{self.model_script_path}'")
		if not self.model_script_path:
			logger.error("PreparationSpec.source_path is empty — nothing to run.")
			return False
		# Model generation needs CAE kernel (mdb) → needs_cae_kernel=True (B6 fix)
		cmd = runner._base_command(self.model_script_path, needs_cae_kernel=True)
		for key, value in self.script_params.items():
			cmd.extend([f'--{key}', str(value)])
		cmd.extend(['--job_name', ctx.job_name])

		# Route through runner._run so timeout, error logging, and
		# record_only dry-run all apply — no strategy calls subprocess directly.
		proc = runner._run(cmd, stage='preparation')
		if proc is None:
			return False
		if runner.record_only:
			return True
		logger.info("Successfully generated model.")
		return os.path.exists(ctx.inp_path)


class ExistingInpStrategy(InpPreparationStrategy):
	"""Preset of :class:`InpPreparationStrategy` that asserts the deck is finished.

	Backs ``kind='existing_inp'``, the UC-03 "pre-existing INP batch" case.
	Mechanically it is the parent with an empty parameter set; what it adds is
	an *assertion*: a ``{{placeholder}}`` anywhere in the tree is reported as
	"you handed a template to a batch of finished decks", which is the mistake
	actually being made, rather than as a missing parameter.

	Attributes
	----------
	source_path : str
		Absolute or relative path to the existing INP file.
	resolve_includes : bool
		If ``True`` (default), rewrite ``*INCLUDE, INPUT=rel_path`` to use
		absolute paths resolved against the referencing file's directory.
	include_staging : str
		See :class:`InpPreparationStrategy`.  Replaces the old ``staging_mode``,
		which asked a per-job question that no longer has one answer.
	"""

	def __init__(
		self,
		source_path: str,
		resolve_includes: bool = True,
		include_staging: str = 'reference',
	):
		super().__init__(source_path, None,
						resolve_includes=resolve_includes,
						assert_finished=True,
						include_staging=include_staging)


# ======================== Compile Strategies ========================

class SubroutineCompileStrategy:
	"""Compiles a user subroutine via ``abaqus make`` before preparation.

	Not part of the :class:`PreparationStrategy`/:class:`ExtractionStrategy`
	ABC hierarchies — compilation is its own concern with a single
	implementation today (YAGNI; add a registry like
	:data:`~ABQflow.core.registry.PREPARATION_REGISTRY` if multiple compile
	backends are ever needed).

	Attributes
	----------
	subroutine : SubroutineSpec
		Subroutine to compile.
	cache : bool
		If ``True`` (default), skip recompilation when the source file's
		content hash matches the last successful compile (see
		:meth:`~ABQflow.core.runner.AbaqusRunner.subroutine_needs_recompile`).
	"""

	def __init__(self, subroutine: SubroutineSpec, cache: bool = True):
		self.subroutine = subroutine
		self.cache = cache

	def compile(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> tuple[bool, str]:
		"""Compile the subroutine, or skip if precompiled/cached.

		Returns
		-------
		tuple[bool, str]
			``(success, message)`` — *message* is empty on success (or a
			skip note), or the compiler's raw stdout+stderr on failure. No
			regex parsing of compiler errors is performed (see
			:meth:`~ABQflow.core.runner.AbaqusRunner.run_compile`).
		"""
		if self.subroutine.precompiled:
			logger.info(f"Subroutine [{self.subroutine.source_path}]: precompiled, skipping compile.")
			return True, ''

		if self.cache and not runner.subroutine_needs_recompile(self.subroutine):
			logger.info(f"Subroutine [{self.subroutine.source_path}]: unchanged, skipping recompile.")
			return True, ''

		logger.info(f"Compiling subroutine [{self.subroutine.source_path}] (solver={self.subroutine.solver})...")
		ok, stdout, stderr = runner.run_compile(self.subroutine)
		if not ok:
			return False, f"{stdout}\n{stderr}".strip()

		if self.cache and not runner.record_only:
			runner._record_compile_hash(self.subroutine)
		logger.info("Subroutine compiled successfully.")
		return True, ''


# ======================== Extraction Strategies ========================
class ExtractionStrategy(ABC):
	"""Interface for extraction: read results from model or ODB files.

	Subclasses
	----------
	OdbExtractionStrategy
		Post-simulation extraction from ODB (requires ``odbAccess``).
	DatExtractionStrategy
		Post-simulation extraction from the printed ``.dat`` output (plain
		text — runs under the host Python, no Abaqus).
	ModelPropertiesExtractionStrategy
		Pre-simulation extraction from INP (requires ``mdb`` / CAE kernel).
	"""

	@abstractmethod
	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Extract results.

		Parameters
		----------
		ctx : JobContext
			Job context providing file paths.
		runner : AbaqusRunner
			Subprocess runner for launching hook scripts.
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		dict
			``{result_name: value, ...}``.  Failed tasks map to ``None``.
		"""
		...


class OdbExtractionStrategy(ExtractionStrategy):
	"""Extract results from the ODB file via hook scripts.

	Runs in the ``odbAccess`` environment (``abaqus python``), NOT the CAE
	kernel.  Each hook script receives ``--odb_path`` as a common argument
	and a JSON task list via ``--tasks_json``.

	Attributes
	----------
	hooks : list[HookSpec]
		List of hook descriptors, each with ``script_path`` and ``tasks``.
	"""

	def __init__(self, hooks: list[HookSpec]):
		self.hooks = hooks

	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Sub strategy [OdbExtract]: Start extracting from ODB...")

		# Ask the runner, not the local filesystem: with a remote backend the
		# .odb only ever exists on the executing machine, and a plain local
		# check would make every remote extraction silently return all-None.
		if not runner.artifact_exists(ctx.odb_path):
			logger.error(f"ODB file does not exist: {ctx.odb_path}")
			all_results = {}
			for hook in self.hooks:
				for task in hook.tasks:
					all_results[task['result_name']] = None
			return all_results

		all_results = {}
		for hook in self.hooks:
			script_path = hook.script_path
			tasks = hook.tasks
			logger.info(f"  -> Run ODB hook script: {script_path} ({len(tasks)} tasks)")
			results = runner.run_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--odb_path': ctx.odb_path},
				needs_cae_kernel=False)   # odbAccess, not mdb
			all_results.update(results)
		return all_results


class DatExtractionStrategy(ExtractionStrategy):
	"""Extract results from the ``.dat`` file via hook scripts.

	An ODB is often too large to open just to read a few numbers; adding
	``*NODE PRINT`` / ``*EL PRINT`` to the deck makes Abaqus write the same
	values as text into ``<job>.dat``, and this strategy reads them back.

	Runs the hook under the **host** Python, not ``abaqus python``: a ``.dat``
	is plain text, so parsing it needs neither the Abaqus kernel nor a license
	token — which is most of the point.  With a remote backend the file is
	fetched into the local job directory first and the hook then runs here;
	shipping a text parser to the solver machine would buy nothing and would
	leave sidecar CSVs on the wrong host.

	Each hook script receives ``--dat_path`` as a common argument and a JSON
	task list via ``--tasks_json``.  ``datkit.py`` is staged alongside
	``hookkit.py`` so hooks can ``import datkit``.

	Attributes
	----------
	hooks : list[HookSpec]
		List of hook descriptors, each with ``script_path`` and ``tasks``.
	"""

	def __init__(self, hooks: list[HookSpec]):
		self.hooks = hooks

	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Sub strategy [DatExtract]: Start extracting from DAT...")

		# The solver writes the .dat on the executing machine, so pull it home
		# before the guard: the hook runs here and opens a local path.
		if runner.is_remote and not runner.record_only:
			runner.fetch_results(patterns=('*.dat',))

		# Plain os.path.exists, deliberately — unlike OdbExtractionStrategy,
		# which asks the runner because the ODB never leaves the remote
		# machine. After the fetch above, *local* existence is the condition
		# that actually decides whether the hook can do its job.
		if not os.path.exists(ctx.dat_path):
			logger.error(f"DAT file does not exist: {ctx.dat_path}")
			all_results = {}
			for hook in self.hooks:
				for task in hook.tasks:
					all_results[task['result_name']] = None
			return all_results

		all_results = {}
		for hook in self.hooks:
			script_path = hook.script_path
			tasks = hook.tasks
			logger.info(f"  -> Run DAT hook script: {script_path} ({len(tasks)} tasks)")
			results = runner.run_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--dat_path': ctx.dat_path},
				needs_cae_kernel=False,
				interpreter='host',            # plain text: no Abaqus needed
				extra_modules=('datkit.py',))
			all_results.update(results)
		return all_results


class ModelPropertiesExtractionStrategy(ExtractionStrategy):
	"""Extract material/property data from the INP *before* simulation.

	Runs in the CAE kernel environment (``abaqus cae noGUI``) because it
	needs ``mdb`` access.  Each hook script receives ``--inp_path`` as a
	common argument and a JSON task list via ``--tasks_json``.

	Attributes
	----------
	hooks : list[HookSpec]
		List of hook descriptors, each with ``script_path`` and ``tasks``.
	"""

	def __init__(self, hooks: list[HookSpec]):
		self.hooks = hooks

	def extract(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info("Sub strategy [ModelPropsExtract]: Start extracting from INP...")

		# Deliberately a *local* check, unlike OdbExtractionStrategy's
		# runner.artifact_exists: the INP is produced here by preparation, so
		# the local copy is the source of truth and "did preparation run?" is
		# the question worth asking.  Asking the executing machine instead
		# would be wrong in the other direction — at this point the deck has
		# not been uploaded yet; run_hook stages it just before the hook runs.
		if not os.path.exists(ctx.inp_path):
			logger.error(f"INP file does not exist: {ctx.inp_path}")
			all_results = {}
			for hook in self.hooks:
				for task in hook.tasks:
					all_results[task['result_name']] = None
			return all_results

		all_results = {}
		for hook in self.hooks:
			script_path = hook.script_path
			tasks = hook.tasks
			logger.info(f"  -> Run model property hook script: {script_path} ({len(tasks)} tasks)")
			results = runner.run_hook(
				script_path=script_path,
				tasks=tasks,
				common_args={'--inp_path': ctx.inp_path},
				needs_cae_kernel=True)    # needs mdb
			all_results.update(results)
		return all_results


# ======================== Workflow Strategies ========================

class JobWorkflowStrategy(ABC):
	"""Interface for a complete job workflow.

	Subclasses
	----------
	MonolithicWorkflowStrategy
		Single-script workflow that handles everything itself.
	ModularWorkflowStrategy
		Multi-phase pipeline: optional subroutine compile, preparation,
		optional preflight, pre-extraction, simulation, post-extraction.

	Optional phase-separated protocol
	----------------------------------
	Subclasses *may* additionally implement ``prepare_only(ctx, runner,
	logger, status_manager=None) -> tuple[dict, JobStatusManager]``,
	``simulate_only(...) -> tuple[dict, JobStatusManager, bool]`` (the
	``bool`` signals whether the pipeline should stop), and
	``extract_only(...) -> tuple[dict, JobStatusManager]`` so that
	:class:`~ABQflow.core.abaqus_automation.AbaqusCalculation` can invoke a
	single phase (see its ``execute(phase=...)`` parameter). This is not
	required by the ABC — :class:`MonolithicWorkflowStrategy` and
	user-defined strategies that don't implement it simply raise
	``NotImplementedError`` when a phase-only call is attempted.
	"""

	@abstractmethod
	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Run the full workflow and return a result dict.

		Parameters
		----------
		ctx : JobContext
			Job context.
		runner : AbaqusRunner
			Subprocess runner for all subprocess calls.
		logger : logging.Logger
			Logger for progress and error messages.

		Returns
		-------
		dict
			Must contain at least a ``'status'`` key (a :class:`JobStatus`
			or its string value).  May include extracted results.
		"""
		...


class MonolithicWorkflowStrategy(JobWorkflowStrategy):
	"""Single-script workflow: one script does everything.

	The script is launched via the CAE kernel (``abaqus cae noGUI``) and
	must print its JSON results wrapped in the sentinel markers
	``===ABQ_RESULT_BEGIN===`` / ``===ABQ_RESULT_END===``.  The result dict
	is expected to contain at least a ``'status'`` key.

	Attributes
	----------
	script_path : str
		Path to the monolithic script.
	params : dict
		Key-value parameters forwarded as ``--key value`` CLI arguments.
	"""

	def __init__(self, script_path: str, params: dict):
		self.script_path = script_path
		self.params = params

	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		logger.info(f"Workflow [MonolithicWorkflow]: Run script '{self.script_path}'")
		# B5/B6 fix: monolithic scripts use CAE kernel (mdb), not 'abaqus python'
		cmd = runner._base_command(self.script_path, needs_cae_kernel=True)
		for key, value in self.params.items():
			cmd.extend([f'--{key}', str(value)])

		# Route through runner._run so timeout, error logging, and
		# record_only dry-run all apply — no strategy calls subprocess directly.
		proc = runner._run(cmd, stage='monolithic')  # B9: error already logged by runner
		if proc is None:
			return {'status': JobStatus.MONOLITHIC_SCRIPT_FAILED,
					'error': f"Monolithic script '{self.script_path}' failed to run (see log for details)."}

		try:
			results = extract_json(proc.stdout)  # B7: sentinel-based extraction
		except (ValueError, json.JSONDecodeError) as e:
			logger.error(f"Unable to decode JSON from script output. Error: {e}")
			return {'status': JobStatus.JSON_DECODE_ERROR, 'error': str(e)}

		if 'status' not in results:
			results['status'] = JobStatus.COMPLETED
		logger.info("Monolithic script run successfully.")
		return results


class ModularWorkflowStrategy(JobWorkflowStrategy):
	"""Multi-phase pipeline: [compile], preparation, [preflight], pre-extraction, simulation, post-extraction.

	Uses a :class:`JobStatusManager` internally to track the job through
	each phase.  If any phase fails the pipeline stops and returns the
	terminal status immediately.

	``execute()`` composes three independently callable phase methods —
	:meth:`prepare_only`, :meth:`simulate_only`, :meth:`extract_only` — so
	that :class:`~ABQflow.core.abaqus_automation.AbaqusCalculation` (and
	:class:`~ABQflow.core.abaqus_automation.BatchAbaqusProcessor`'s
	``run_preparation``/``run_simulation``/``run_extraction``) can invoke a
	single phase without running the rest of the pipeline. The external
	contract of ``execute()`` — return-dict shape and terminal-status
	semantics — is unchanged by this split.

	Attributes
	----------
	preparation_strategy : PreparationStrategy
		Strategy that produces the INP file.
	preflight_mode : str or None
		``'syntaxcheck'``, ``'datacheck'``, or ``None`` (IMP-04).
	pre_extraction_strategies : list[ExtractionStrategy]
		Strategies run before the solver (e.g. property extraction from INP).
	post_extraction_strategies : list[ExtractionStrategy]
		Strategies run after the solver (e.g. result extraction from ODB).
	compile_strategy : SubroutineCompileStrategy or None
		Optional user-subroutine compile step, run before preparation.
	"""

	def __init__(
		self,
		preparation_strategy: PreparationStrategy,
		pre_extraction_strategies: List[ExtractionStrategy],
		post_extraction_strategies: List[ExtractionStrategy],
		preflight_mode: str | None = None,
		preflight_only: bool = False,
		compile_strategy: SubroutineCompileStrategy | None = None,
	):
		self.preparation_strategy = preparation_strategy
		self.preflight_mode = preflight_mode
		self.pre_extraction_strategies = pre_extraction_strategies
		self.post_extraction_strategies = post_extraction_strategies
		self.preflight_only = preflight_only
		self.compile_strategy = compile_strategy

	def prepare_only(self, ctx: JobContext, runner: AbaqusRunner, logger: logging.Logger,
					status_manager: JobStatusManager | None = None) -> tuple[dict, JobStatusManager]:
		"""Phase 1: optional subroutine compile, preparation, optional preflight.

		Standalone entry point for "produce an INP (and compiled
		subroutine) only". Does not run pre-extraction, the solver, or
		post-extraction — those live in :meth:`simulate_only` /
		:meth:`extract_only`.

		Returns
		-------
		tuple[dict, JobStatusManager]
			``(results, status_manager)`` — *results* has at least
			``'status'`` and ``'_phase_history'``; the manager is returned
			so :meth:`execute` can thread it into the next phase.
		"""
		logger.info("Workflow Strategy [ModularWorkflow]: prepare_only phase...")
		sm = status_manager or JobStatusManager()
		results: dict = {}

		# 0. Subroutine compilation (optional)
		if self.compile_strategy is not None:
			sm.mark_compiling()
			ok, msg = self.compile_strategy.compile(ctx, runner, logger)
			sm.record_compile(success=ok, error=None if ok else msg)
			if not ok:
				results['status'] = sm.get_final_status()
				results['_phase_history'] = sm.phase_history
				return results, sm

		# 1. Preparation
		sm.mark_preparing()
		if not self.preparation_strategy.prepare(ctx, runner, logger):
			sm.record_preparation(success=False)
			results['status'] = sm.get_final_status()
			results['_phase_history'] = sm.phase_history
			return results, sm
		sm.record_preparation(success=True)

		# 2. Preflight (IMP-04: inserted before pre-extraction for fail-fast)
		if self.preflight_mode:
			logger.info(f"Preflight [{self.preflight_mode}]: checking INP...")
			sm.mark_preflight()
			passed, pf_errors = runner.run_preflight(self.preflight_mode)
			if not passed:
				sm.record_preflight(
					success=False,
					error=pf_errors[0] if pf_errors else f"Preflight [{self.preflight_mode}] failed",
				)
				results['status'] = sm.get_final_status()
				results['_phase_history'] = sm.phase_history
				return results, sm
			sm.record_preflight(success=True)
			logger.info(f"Preflight [{self.preflight_mode}]: passed")

		results['status'] = sm.get_final_status()
		results['_phase_history'] = sm.phase_history
		return results, sm

	def simulate_only(self, ctx: JobContext, runner: AbaqusRunner, logger: logging.Logger,
					status_manager: JobStatusManager | None = None) -> tuple[dict, JobStatusManager, bool]:
		"""Phase 2: pre-extraction hooks, then the solver run.

		Assumes ``ctx.inp_path`` already exists (produced by a prior
		:meth:`prepare_only` call — possibly in an earlier process/session,
		e.g. via ``BatchAbaqusProcessor.run_simulation()``). Mirrors the
		original monolithic behavior: a pre-extraction failure does *not*
		stop the solver from running, but a solver failure does stop the
		pipeline.

		Returns
		-------
		tuple[dict, JobStatusManager, bool]
			``(results, status_manager, stop)`` — *stop* is ``True`` when
			the caller (e.g. :meth:`execute`) should not proceed to
			:meth:`extract_only` (INP missing or solver failed).
		"""
		logger.info("Workflow Strategy [ModularWorkflow]: simulate_only phase...")
		sm = status_manager or JobStatusManager()
		results: dict = {}

		if not os.path.exists(ctx.inp_path):
			sm.record_simulation(
				success=False,
				error=f"INP not found: {ctx.inp_path} (run preparation first)",
			)
			results['status'] = sm.get_final_status()
			results['_phase_history'] = sm.phase_history
			return results, sm, True

		# 3. Pre-extraction
		for strategy in self.pre_extraction_strategies:
			sm.mark_extracting('pre_extraction')
			pre_ext_results = strategy.extract(ctx, runner, logger)
			sm.record_extraction(pre_ext_results)
			results.update(pre_ext_results)

		# 4. Simulation (IMP-02: diagnostics-backed verdict)
		sm.mark_simulating()
		solver_result = runner.run_solver()
		# Attach diagnostics on failure and on the rc≠0+COMPLETED edge case
		if solver_result.diagnostics is not None:
			if not solver_result.success or solver_result.error:
				results['diagnostics'] = asdict(solver_result.diagnostics)
		if not solver_result.success:
			sm.record_simulation(success=False, error=solver_result.error)
			results['status'] = sm.get_final_status()
			results['_phase_history'] = sm.phase_history
			return results, sm, True
		sm.record_simulation(success=True)

		results['status'] = sm.get_final_status()
		results['_phase_history'] = sm.phase_history
		return results, sm, False

	def extract_only(self, ctx: JobContext, runner: AbaqusRunner, logger: logging.Logger,
					status_manager: JobStatusManager | None = None) -> tuple[dict, JobStatusManager]:
		"""Phase 3: post-extraction hooks only.

		Assumes ``ctx.odb_path`` already exists. No existence guard is
		needed — :class:`OdbExtractionStrategy` already reports every task
		as ``None`` when the ODB is missing, and :meth:`JobStatusManager.record_extraction`
		already turns that into ``EXTRACTION_FAILED``.

		Returns
		-------
		tuple[dict, JobStatusManager]
			``(results, status_manager)``.
		"""
		logger.info("Workflow Strategy [ModularWorkflow]: extract_only phase...")
		sm = status_manager or JobStatusManager()
		results: dict = {}

		# 5. Post-extraction
		for strategy in self.post_extraction_strategies:
			sm.mark_extracting('post_extraction')
			post_ext_results = strategy.extract(ctx, runner, logger)
			sm.record_extraction(post_ext_results)
			results.update(post_ext_results)

		results['status'] = sm.get_final_status()
		results['_phase_history'] = sm.phase_history
		return results, sm

	def execute(self, ctx: JobContext, runner: AbaqusRunner,
				logger: logging.Logger) -> dict:
		"""Run the full modular workflow by composing the three phase methods.

		Returns a dict with at least a ``'status'`` key plus any results
		from pre- and post-extraction hooks.  Failing early means later
		phases are skipped — return-dict shape and terminal-status
		semantics are unchanged from before the phase-separation refactor.
		"""
		logger.info("Workflow Strategy [ModularWorkflow]: Starting Modular Workflow...")

		results, sm = self.prepare_only(ctx, runner, logger)
		if sm.get_final_status() != JobStatus.COMPLETED:
			return results
		# IMP-04: preflight_only mode — stop after preflight, skip solver & extraction
		if self.preflight_only:
			return results

		sim_results, sm, stop = self.simulate_only(ctx, runner, logger, status_manager=sm)
		results.update(sim_results)
		if stop:
			return results

		ext_results, sm = self.extract_only(ctx, runner, logger, status_manager=sm)
		results.update(ext_results)
		return results