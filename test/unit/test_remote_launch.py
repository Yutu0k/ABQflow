"""Pure builders for detached remote execution — strings in, strings out.

No network, no Abaqus, no filesystem.  Several of these guard mistakes that
were made for real during the remote spike and cost a full debugging cycle
each; those are called out individually.

Run: pytest test/unit/test_remote_launch.py -v
"""

from __future__ import annotations

import base64

from ABQflow.core.remote_launch import (
	build_cmd_line,
	build_detach_script,
	build_launcher_bat,
	find_includes,
	grace_period,
	parse_detach_output,
	parse_rc_sentinel,
	poll_verdict,
	quote_arg,
	rewrite_includes,
	wrap_cmd,
	wrap_powershell,
)

ABQ = r'C:\Program Files\SIMULIA\Commands\abaqus.bat'


# ============================================================
# launcher .bat
# ============================================================

def test_bat_changes_to_the_job_directory():
	bat = build_launcher_bat(ABQ, 'myjob', r'D:\w\myjob', 4)
	assert 'cd /d "D:\\w\\myjob"' in bat


def test_bat_keeps_interactive_so_errorlevel_is_the_solvers():
	bat = build_launcher_bat(ABQ, 'myjob', r'D:\w\myjob', 4)
	assert 'interactive' in bat


def test_bat_writes_rc_sentinel_after_the_solver_exits():
	bat = build_launcher_bat(ABQ, 'myjob', r'D:\w\myjob', 4)
	assert bat.index('interactive') < bat.index('abqflow.rc')


def test_bat_avoids_the_cmd_file_descriptor_trap():
	"""Regression: ``echo %ERRORLEVEL%> f`` expands to ``echo 0> f``.

	cmd.exe reads a digit immediately before ``>`` as a file-descriptor
	number, so it redirects stdin and the sentinel ends up with no return
	code at all — observed in practice as ``returncode: None`` on jobs that
	had in fact succeeded.  The parentheses keep the digit away from ``>``.
	"""
	bat = build_launcher_bat(ABQ, 'myjob', r'D:\w\myjob', 4)
	assert '(echo %ERRORLEVEL%)>' in bat
	assert 'echo %ERRORLEVEL%>' not in bat.replace('(echo %ERRORLEVEL%)>', '')


def test_bat_quotes_an_abaqus_path_containing_spaces():
	bat = build_launcher_bat(ABQ, 'j', r'D:\w', 2)
	assert f'call "{ABQ}"' in bat


def test_bat_uses_crlf():
	assert build_launcher_bat(ABQ, 'j', r'D:\w', 2).count('\r\n') >= 4


def test_bat_includes_user_subroutine_when_given():
	bat = build_launcher_bat(ABQ, 'j', r'D:\w', 2, user_subroutine=r'D:\w\umat.obj')
	assert 'user="D:\\w\\umat.obj"' in bat


def test_bat_omits_user_when_absent():
	assert 'user=' not in build_launcher_bat(ABQ, 'j', r'D:\w', 2)


# ============================================================
# detach launcher
# ============================================================

def test_detach_script_uses_win32_process_create():
	script = build_detach_script(r'D:\w\run.bat', r'D:\w')
	assert 'Win32_Process' in script and 'Create' in script
	assert 'D:\\w\\run.bat' in script


def test_detach_script_escapes_single_quotes():
	script = build_detach_script(r"D:\o'brien\run.bat", r"D:\o'brien")
	assert "o''brien" in script


def test_parse_detach_output_reads_returnvalue_and_pid():
	assert parse_detach_output('0\r\n12345\r\n') == (0, 12345)


def test_parse_detach_output_handles_failure_without_pid():
	assert parse_detach_output('8\r\n') == (8, None)


def test_parse_detach_output_tolerates_empty():
	assert parse_detach_output('') == (None, None)
	assert parse_detach_output(None) == (None, None)


def test_parse_detach_output_ignores_banner_noise():
	assert parse_detach_output('WARNING: something\n0\n999\n') == (0, 999)


# ============================================================
# rc sentinel
# ============================================================

def test_parse_rc_sentinel_reads_a_number():
	assert parse_rc_sentinel('0\r\n') == 0
	assert parse_rc_sentinel(' 7 ') == 7


def test_parse_rc_sentinel_on_missing_or_garbage():
	assert parse_rc_sentinel(None) is None
	assert parse_rc_sentinel('ECHO is on.') is None


# ============================================================
# poll truth table
# ============================================================

def test_no_rc_and_no_lck_early_is_running_not_finished():
	"""The startup race: Abaqus does not create .lck for a few seconds.

	Measured: absent at t=2 s, present at t=5 s.  Treating "no lck" as done
	would report success before the solver had started.
	"""
	assert poll_verdict(False, False, 1.0, 600) == 'running'


def test_lck_present_is_running():
	assert poll_verdict(False, True, 5.0, 600) == 'running'


def test_rc_present_wins_even_while_lck_lingers():
	assert poll_verdict(True, True, 5.0, 600) == 'finished'


def test_elapsed_past_timeout_is_timeout():
	assert poll_verdict(False, False, 601.0, 600) == 'timeout'


def test_rc_beats_an_expired_timeout():
	assert poll_verdict(True, False, 601.0, 600) == 'finished'


def test_no_timeout_never_expires():
	assert poll_verdict(False, True, 1e9, None) == 'running'


# ============================================================
# grace period
# ============================================================

def test_grace_period_bounds():
	assert grace_period(None) == 300
	assert grace_period(100) == 30       # clamped to the floor
	assert grace_period(2000) == 100     # 5% of T, inside the range
	assert grace_period(100000) == 300   # clamped to the ceiling


# ============================================================
# Windows quoting
# ============================================================

def test_quote_arg_leaves_simple_args_alone():
	assert quote_arg('cpus=4') == 'cpus=4'


def test_quote_arg_quotes_spaces():
	assert quote_arg(r'C:\Program Files\a.bat') == r'"C:\Program Files\a.bat"'


def test_build_cmd_line_quotes_only_what_needs_it():
	assert build_cmd_line(['a', 'b c']) == 'a "b c"'


def test_wrap_cmd_uses_the_slash_s_form():
	"""``cmd /s`` has the one well-defined rule: strip the outer quote pair."""
	line = wrap_cmd([ABQ, 'information=release'])
	assert line.startswith('cmd /s /c "') and line.endswith('"')
	inner = line[len('cmd /s /c "'):-1]
	assert inner == f'"{ABQ}" information=release'


def test_wrap_cmd_can_prepend_a_cd():
	line = wrap_cmd(['abaqus', 'python', 'h.py'], cwd=r'D:\w\j')
	assert 'cd /d "D:\\w\\j" &&' in line


def test_wrap_powershell_is_quote_free():
	"""Base64 carries no quotes, so it survives any intermediate shell."""
	line = wrap_powershell('Write-Output "hi"')
	assert '"' not in line
	assert line.startswith('powershell -NoProfile -NonInteractive -EncodedCommand ')


def test_wrap_powershell_round_trips():
	line = wrap_powershell('Write-Output "hi"')
	decoded = base64.b64decode(line.rsplit(' ', 1)[1]).decode('utf-16-le')
	assert decoded == 'Write-Output "hi"'


# ============================================================
# *INCLUDE parsing and substitution (placement lives in the runner)
# ============================================================

def test_find_includes_on_a_deck_without_any():
	assert find_includes('*Heading\n*Node\n1, 0., 0.\n') == []


def test_include_matching_is_case_insensitive():
	assert find_includes('*include, input=sub/frag.inp\n') == ['sub/frag.inp']


def test_find_includes_reads_a_quoted_path_with_spaces():
	assert find_includes('*INCLUDE, INPUT="C:\\deep dir\\frag.inp"\n') == \
		['C:\\deep dir\\frag.inp']


def test_find_includes_handles_several():
	text = ('*INCLUDE, INPUT=a/one.inp\n'
			'*Step\n'
			'*INCLUDE, INPUT=b/two.inp\n')
	assert find_includes(text) == ['a/one.inp', 'b/two.inp']


def test_rewrite_substitutes_each_directive_through_the_resolver():
	text = '*Heading\n*INCLUDE, INPUT=parts/mesh.inp\n*End\n'
	out = rewrite_includes(text, lambda raw: r'D:\shared\abc_mesh.inp')
	assert find_includes(out) == [r'D:\shared\abc_mesh.inp']


def test_rewrite_leaves_a_directive_the_resolver_declines():
	"""None means "I have no answer" — silently rewriting it to something that
	does not exist would be worse than leaving it alone."""
	text = '*INCLUDE, INPUT=a/one.inp\n*INCLUDE, INPUT=b/two.inp\n'
	out = rewrite_includes(text, lambda raw: None if 'one' in raw else 'two.inp')
	assert find_includes(out) == ['a/one.inp', 'two.inp']


def test_rewrite_leaves_a_deck_without_includes_untouched():
	text = '*Heading\n*Node\n'
	assert rewrite_includes(text, lambda raw: 'x') == text
