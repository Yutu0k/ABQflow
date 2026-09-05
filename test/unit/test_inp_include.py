"""Tests for ABQflow.core.inp_include and the two strategies built on it.

The behaviour under test is the two-tier split: a file in the ``*INCLUDE``
tree that carries ``{{placeholders}}`` (or whose descendants do) becomes a
per-job copy named by a bare filename, and everything else stays where it is
and is named by an absolute path.  Everything runs on tmp_path files — no
Abaqus, no subprocess.

Run: pytest test/unit/test_inp_include.py -v
"""

import logging
import os

import pytest

from ABQflow import JobSpec, PreparationSpec, build_workflow
from ABQflow.core.context import JobContext
from ABQflow.core.inp_include import (
	INCLUDE_RE,
	IncludeResolutionError,
	resolve_include_tree,
	resolve_target,
)
from ABQflow.core.strategies import (
	ExistingInpStrategy,
	InpModifyStrategy,
	InpPreparationStrategy,
)


@pytest.fixture
def logger():
	log = logging.getLogger('test_inp_include')
	log.addHandler(logging.NullHandler())
	return log


def _w(root, rel, text):
	"""Write *text* to ``root/rel``, creating parents; return the path as str.

	``newline=''`` because the walker preserves line endings byte for byte —
	letting Windows translate ``\\n`` here would test the platform, not the code.
	"""
	path = root / rel
	path.parent.mkdir(parents=True, exist_ok=True)
	path.write_text(text, encoding='utf-8', newline='')
	return str(path)


def _includes(text):
	"""The raw INPUT= values of *text*, in order."""
	return [m.group(3).strip() for m in INCLUDE_RE.finditer(text)]


# ============================================================
# Target resolution
# ============================================================

def test_relative_target_resolves_against_the_referencing_file(tmp_path):
	base = str(tmp_path / 'cae' / 'scenarios')
	assert resolve_target('../main.inp', base) == os.path.normpath(
		str(tmp_path / 'cae' / 'main.inp'))


def test_absolute_target_is_kept(tmp_path):
	target = os.path.normpath(str(tmp_path / 'mesh.inp'))
	assert resolve_target(target, str(tmp_path / 'elsewhere')) == target


def test_separators_are_normalised_both_ways(tmp_path):
	base = str(tmp_path)
	assert resolve_target('parts/mesh.inp', base) == resolve_target(
		'parts\\mesh.inp', base)


# ============================================================
# Static includes stay shared
# ============================================================

def test_static_include_is_rewritten_to_an_absolute_path(tmp_path, logger):
	mesh = _w(tmp_path, 'cae/mesh.inp', '*Node\n1, 0., 0.\n')
	root = _w(tmp_path, 'cae/scenarios/s1.inp',
			'*Include, input=../mesh.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {}, logger=logger)

	assert _includes(res.root_text) == [os.path.normpath(mesh)]
	assert res.materialized == {}
	assert res.shared == (os.path.normpath(mesh),)


def test_nested_static_includes_are_all_followed(tmp_path, logger):
	"""The gap the flat rewrite left: a second-level include was never seen."""
	leaf = _w(tmp_path, 'parts/leaf.inp', '*Node\n1, 0., 0.\n')
	mid = _w(tmp_path, 'parts/mid.inp', '*Include, input=leaf.inp\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=parts/mid.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {}, logger=logger)

	assert set(res.shared) == {os.path.normpath(mid), os.path.normpath(leaf)}


def test_step_is_found_in_an_included_fragment(tmp_path, logger):
	_w(tmp_path, 'step.inp', '*Step\n*Static\n*End Step\n')
	root = _w(tmp_path, 'root.inp', '*Heading\n*Include, input=step.inp\n')

	assert resolve_include_tree(root, {}, logger=logger).has_step


def test_a_tree_without_step_reports_so(tmp_path, logger):
	_w(tmp_path, 'mesh.inp', '*Node\n1, 0., 0.\n')
	root = _w(tmp_path, 'root.inp', '*Heading\n*Include, input=mesh.inp\n')

	assert not resolve_include_tree(root, {}, logger=logger).has_step


# ============================================================
# Parameterized includes become per-job copies
# ============================================================

def test_parameterized_include_is_materialized_under_a_bare_name(tmp_path, logger):
	_w(tmp_path, 'frag/material.inp', '*Elastic\n{{youngs_modulus}}, 0.3\n')
	root = _w(tmp_path, 'root.inp',
			'*Include, input=frag/material.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {'youngs_modulus': 210000}, logger=logger)

	assert _includes(res.root_text) == ['material.inp']
	assert res.materialized == {'material.inp': '*Elastic\n210000, 0.3\n'}
	assert res.shared == ()


def test_mixed_tree_splits_shared_from_per_job(tmp_path, logger):
	"""The target scenario: one scenario deck, one shared mesh, one template."""
	mesh = _w(tmp_path, 'cae/main.inp', '*Node\n1, 0., 0.\n')
	_w(tmp_path, 'cae/material_template.inp', '*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'cae/scenario.inp',
			'*Include, input=main.inp\n'
			'*Include, input=material_template.inp\n'
			'*Step\n*Dsload\nSurf, P, -{{load}}\n*End Step\n')

	res = resolve_include_tree(root, {'E': 200000, 'load': 3000}, logger=logger)

	assert _includes(res.root_text) == [os.path.normpath(mesh), 'material_template.inp']
	assert res.materialized == {'material_template.inp': '*Elastic\n200000, 0.3\n'}
	assert res.shared == (os.path.normpath(mesh),)
	assert '-3000' in res.root_text
	assert res.placeholders == frozenset({'E', 'load'})


def test_a_static_parent_of_a_parameterized_child_is_also_materialized(tmp_path, logger):
	"""Taint propagates upward: the parent's own directive has to be rewritten,
	so the parent cannot stay shared even though its own text has no markers."""
	_w(tmp_path, 'frag/inner.inp', '*Elastic\n{{E}}, 0.3\n')
	_w(tmp_path, 'frag/outer.inp', '*Material, name=M\n*Include, input=inner.inp\n')
	root = _w(tmp_path, 'root.inp',
			'*Include, input=frag/outer.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {'E': 1.5}, logger=logger)

	assert _includes(res.root_text) == ['outer.inp']
	assert sorted(res.materialized) == ['inner.inp', 'outer.inp']
	assert _includes(res.materialized['outer.inp']) == ['inner.inp']
	assert res.materialized['inner.inp'] == '*Elastic\n1.5, 0.3\n'


def test_a_parameterized_subtree_keeps_its_own_static_include_shared(tmp_path, logger):
	mesh = _w(tmp_path, 'mesh.inp', '*Node\n1, 0., 0.\n')
	_w(tmp_path, 'tpl.inp', '*Include, input=mesh.inp\n*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=tpl.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {'E': 7}, logger=logger)

	assert _includes(res.materialized['tpl.inp']) == [os.path.normpath(mesh)]
	assert res.shared == (os.path.normpath(mesh),)


# ============================================================
# Placeholder coverage across the tree
# ============================================================

def test_placeholders_are_collected_from_every_file(tmp_path, logger):
	_w(tmp_path, 'a.inp', '*Elastic\n{{E}}, {{nu}}\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=a.inp\n*Step\n{{dt}}\n*End Step\n')

	res = resolve_include_tree(root, {'E': 1, 'nu': 2, 'dt': 3}, logger=logger)

	assert res.placeholders == frozenset({'E', 'nu', 'dt'})


def test_an_uncovered_placeholder_is_left_intact_for_the_caller_to_report(tmp_path, logger):
	"""Substitution must not raise on the first miss — the strategy reports the
	whole missing set at once, and nothing is written when it does."""
	root = _w(tmp_path, 'root.inp', '*Step\n{{known}} {{missing}}\n*End Step\n')

	res = resolve_include_tree(root, {'known': 5}, logger=logger)

	assert '5 {{missing}}' in res.root_text
	assert res.placeholders == frozenset({'known', 'missing'})


# ============================================================
# Naming, dedup, and failure modes
# ============================================================

def test_two_includes_sharing_a_basename_do_not_overwrite_each_other(tmp_path, logger):
	_w(tmp_path, 'x/frag.inp', '*Elastic\n{{a}}, 0.3\n')
	_w(tmp_path, 'y/frag.inp', '*Plastic\n{{b}}, 0.\n')
	root = _w(tmp_path, 'root.inp',
			'*Include, input=x/frag.inp\n*Include, input=y/frag.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {'a': 1, 'b': 2}, logger=logger)

	names = _includes(res.root_text)
	assert names[0] == 'frag.inp'
	assert names[1].startswith('frag_') and names[1].endswith('.inp')
	assert sorted(res.materialized) == sorted(names)
	assert len(res.materialized) == 2


def test_a_basename_colliding_with_the_job_inp_is_renamed(tmp_path, logger):
	_w(tmp_path, 'frag/job.inp', '*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=frag/job.inp\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {'E': 1}, reserved_names=('job.inp',), logger=logger)

	assert _includes(res.root_text) != ['job.inp']
	assert list(res.materialized) == _includes(res.root_text)


def test_a_file_included_twice_is_materialized_once(tmp_path, logger):
	_w(tmp_path, 'frag.inp', '*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'root.inp',
			'*Include, input=frag.inp\n*Step\n*Include, input=./frag.inp\n*End Step\n')

	res = resolve_include_tree(root, {'E': 1}, logger=logger)

	assert _includes(res.root_text) == ['frag.inp', 'frag.inp']
	assert list(res.materialized) == ['frag.inp']


def test_a_missing_target_names_the_file_that_referenced_it(tmp_path, logger):
	root = _w(tmp_path, 'deep/root.inp', '*Include, input=../absent.inp\n*Step\n*End Step\n')

	with pytest.raises(IncludeResolutionError) as excinfo:
		resolve_include_tree(root, {}, logger=logger)

	message = str(excinfo.value)
	assert 'absent.inp' in message
	assert 'root.inp' in message


def test_a_cycle_is_reported_rather_than_recursed_into(tmp_path, logger):
	_w(tmp_path, 'a.inp', '*Include, input=b.inp\n')
	_w(tmp_path, 'b.inp', '*Include, input=a.inp\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=a.inp\n*Step\n*End Step\n')

	with pytest.raises(IncludeResolutionError, match='cycle'):
		resolve_include_tree(root, {}, logger=logger)


def test_follow_includes_false_leaves_directives_as_authored(tmp_path, logger):
	root = _w(tmp_path, 'root.inp',
			'*Include, input=nowhere/at/all.inp\n*Step\n{{E}}\n*End Step\n')

	res = resolve_include_tree(root, {'E': 3}, follow_includes=False, logger=logger)

	assert _includes(res.root_text) == ['nowhere/at/all.inp']
	assert '3' in res.root_text
	assert res.materialized == {}


def test_quoted_and_uppercase_directives_are_matched(tmp_path, logger):
	mesh = _w(tmp_path, 'a dir/mesh.inp', '*Node\n1, 0., 0.\n')
	root = _w(tmp_path, 'root.inp',
			'*INCLUDE, INPUT="a dir/mesh.inp"\n*Step\n*End Step\n')

	res = resolve_include_tree(root, {}, logger=logger)

	assert res.shared == (os.path.normpath(mesh),)


# ============================================================
# Strategy level
# ============================================================

def _ctx(tmp_path, name='job'):
	out = tmp_path / 'out'
	out.mkdir(exist_ok=True)
	return JobContext(job_name=name, output_dir=str(out), cpus=1)


def test_inp_modify_writes_the_root_and_its_per_job_includes(tmp_path, logger):
	mesh = _w(tmp_path, 'src/main.inp', '*Node\n1, 0., 0.\n')
	_w(tmp_path, 'src/material_template.inp', '*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'src/scenario.inp',
			'*Include, input=main.inp\n'
			'*Include, input=material_template.inp\n'
			'*Step\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert InpModifyStrategy(root, {'E': 200000}).prepare(ctx, None, logger)

	written = open(ctx.inp_path, encoding='utf-8').read()
	assert _includes(written) == [os.path.normpath(mesh), 'material_template.inp']
	local = os.path.join(ctx.output_dir, 'material_template.inp')
	assert open(local, encoding='utf-8').read() == '*Elastic\n200000, 0.3\n'


def test_inp_modify_fails_on_a_placeholder_missing_from_an_include(tmp_path, logger):
	_w(tmp_path, 'src/frag.inp', '*Elastic\n{{E}}, {{nu}}\n')
	root = _w(tmp_path, 'src/root.inp', '*Include, input=frag.inp\n*Step\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert not InpModifyStrategy(root, {'E': 1}).prepare(ctx, None, logger)
	assert not os.path.exists(ctx.inp_path)
	assert not os.path.exists(os.path.join(ctx.output_dir, 'frag.inp'))


def test_inp_modify_without_includes_is_unchanged(tmp_path, logger):
	root = _w(tmp_path, 'src/root.inp', '*Step\n*Elastic\n{{E}}, 0.3\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert InpModifyStrategy(root, {'E': 5}).prepare(ctx, None, logger)
	assert open(ctx.inp_path, encoding='utf-8').read() == (
		'*Step\n*Elastic\n5, 0.3\n*End Step\n')


def test_inp_modify_reports_a_missing_include_instead_of_writing_a_broken_deck(tmp_path, logger):
	root = _w(tmp_path, 'src/root.inp', '*Include, input=gone.inp\n*Step\n{{E}}\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert not InpModifyStrategy(root, {'E': 1}).prepare(ctx, None, logger)
	assert not os.path.exists(ctx.inp_path)


def test_existing_inp_accepts_a_step_that_lives_in_an_include(tmp_path, logger):
	_w(tmp_path, 'src/step.inp', '*Step\n*Static\n*End Step\n')
	root = _w(tmp_path, 'src/root.inp', '*Heading\n*Include, input=step.inp\n')
	ctx = _ctx(tmp_path)

	assert ExistingInpStrategy(root).prepare(ctx, None, logger)


def test_existing_inp_rejects_a_placeholder_hiding_in_an_include(tmp_path, logger):
	_w(tmp_path, 'src/frag.inp', '*Elastic\n{{E}}, 0.3\n')
	root = _w(tmp_path, 'src/root.inp', '*Include, input=frag.inp\n*Step\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert not ExistingInpStrategy(root).prepare(ctx, None, logger)
	assert not os.path.exists(ctx.inp_path)


def test_existing_inp_materializes_nothing(tmp_path, logger):
	mesh = _w(tmp_path, 'src/mesh.inp', '*Node\n1, 0., 0.\n')
	root = _w(tmp_path, 'src/root.inp', '*Include, input=mesh.inp\n*Step\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert ExistingInpStrategy(root).prepare(ctx, None, logger)
	assert os.listdir(ctx.output_dir) == ['job.inp']
	assert _includes(open(ctx.inp_path, encoding='utf-8').read()) == [os.path.normpath(mesh)]


# ============================================================
# The two kinds are one implementation with two presets
# ============================================================

def test_both_kinds_share_one_implementation():
	assert issubclass(InpModifyStrategy, InpPreparationStrategy)
	assert issubclass(ExistingInpStrategy, InpPreparationStrategy)


def test_the_presets_differ_only_in_the_finished_assertion(tmp_path):
	root = _w(tmp_path, 'root.inp', '*Step\n*End Step\n')
	assert not InpModifyStrategy(root, {}).assert_finished
	assert ExistingInpStrategy(root).assert_finished


def test_a_leftover_placeholder_is_reported_differently_per_preset(tmp_path, caplog):
	"""Same defect, two mistakes: a missing parameter, or a template handed to
	a batch of finished decks. The message has to name the right one."""
	root = _w(tmp_path, 'root.inp', '*Step\n{{E}}\n*End Step\n')
	log = logging.getLogger('preset_messages')

	with caplog.at_level(logging.ERROR, logger='preset_messages'):
		assert not InpModifyStrategy(root, {}).prepare(_ctx(tmp_path, 'a'), None, log)
	assert 'missing parameters' in caplog.text

	caplog.clear()
	with caplog.at_level(logging.ERROR, logger='preset_messages'):
		assert not ExistingInpStrategy(root).prepare(_ctx(tmp_path, 'b'), None, log)
	assert 'looks like a template' in caplog.text


def test_inp_based_now_rejects_a_deck_with_no_step(tmp_path, logger):
	"""Behaviour change: the completeness check used to be existing_inp's alone,
	which let a broken template fail later, inside the solver."""
	root = _w(tmp_path, 'root.inp', '*Heading\n*Elastic\n{{E}}, 0.3\n')
	ctx = _ctx(tmp_path)

	assert not InpModifyStrategy(root, {'E': 1}).prepare(ctx, None, logger)
	assert not os.path.exists(ctx.inp_path)


# ============================================================
# include_staging / option validation
# ============================================================

def test_include_staging_defaults_to_reference(tmp_path, logger):
	mesh = _w(tmp_path, 'mesh.inp', '*Node\n1, 0., 0.\n')
	root = _w(tmp_path, 'root.inp', '*Include, input=mesh.inp\n*Step\n*End Step\n')
	ctx = _ctx(tmp_path)

	assert InpModifyStrategy(root, {}).prepare(ctx, None, logger)
	# referenced, not copied
	assert os.listdir(ctx.output_dir) == ['job.inp']
	assert _includes(open(ctx.inp_path, encoding='utf-8').read()) == [os.path.normpath(mesh)]


def test_a_planned_but_unbuilt_include_staging_says_so(tmp_path, caplog):
	root = _w(tmp_path, 'root.inp', '*Step\n*End Step\n')
	log = logging.getLogger('staging')

	with caplog.at_level(logging.ERROR, logger='staging'):
		ok = InpModifyStrategy(root, {}, include_staging='copy').prepare(
			_ctx(tmp_path), None, log)

	assert not ok
	assert 'not implemented' in caplog.text


def test_an_unknown_include_staging_is_rejected(tmp_path, caplog):
	root = _w(tmp_path, 'root.inp', '*Step\n*End Step\n')
	log = logging.getLogger('staging2')

	with caplog.at_level(logging.ERROR, logger='staging2'):
		ok = InpModifyStrategy(root, {}, include_staging='nonsense').prepare(
			_ctx(tmp_path), None, log)

	assert not ok
	assert 'Unknown include_staging' in caplog.text


@pytest.mark.parametrize('kind', ['inp_based', 'existing_inp'])
def test_an_unknown_preparation_option_raises_instead_of_being_ignored(kind):
	"""The trap this closes: an option key the strategy does not understand used
	to be dropped in silence, and the batch ran with a setting the user thought
	was on.  ``staging_mode`` is the concrete case — it was removed, and a config
	still carrying it must say so rather than quietly changing meaning."""
	spec = JobSpec('j', preparation=PreparationSpec(
		kind=kind, source_path='x.inp',
		options={'staging_mode': 'copy'}))

	with pytest.raises(ValueError, match='Unknown preparation option'):
		build_workflow(spec)


def test_a_misspelled_option_raises():
	spec = JobSpec('j', preparation=PreparationSpec(
		kind='inp_based', source_path='x.inp',
		options={'resolve_include': False}))

	with pytest.raises(ValueError, match='resolve_include'):
		build_workflow(spec)


def test_existing_inp_options_reach_the_strategy_through_the_registry():
	spec = JobSpec('j', preparation=PreparationSpec(
		kind='existing_inp', source_path='x.inp',
		options={'resolve_includes': False}))

	strategy = build_workflow(spec).preparation_strategy
	assert isinstance(strategy, ExistingInpStrategy)
	assert strategy.resolve_includes is False
	assert strategy.assert_finished


def test_options_reach_the_strategy_through_the_registry():
	spec = JobSpec('j', preparation=PreparationSpec(
		kind='inp_based', source_path='x.inp',
		options={'resolve_includes': False, 'include_staging': 'reference'}))

	strategy = build_workflow(spec).preparation_strategy
	assert isinstance(strategy, InpModifyStrategy)
	assert strategy.resolve_includes is False
	assert strategy.include_staging == 'reference'
