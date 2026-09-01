"""INP reading must not depend on the machine's locale.

Python's text mode defaults to the system encoding — GBK on a Chinese
Windows, cp1252 elsewhere.  A UTF-8 byte-order mark is enough to make a plain
``open(path)`` raise there, which is how a perfectly ordinary deck exported
by an editor became unreadable: ``'gbk' codec can't decode byte 0xbf in
position 2``.  Every job in the batch failed in preparation with that.

Run: pytest test/unit/test_inp_encoding.py -v
"""

from __future__ import annotations

import codecs

import pytest

from ABQflow.core.strategies import read_inp_text, write_inp_text

_DECK = '*Heading\n** a comment\n*Step, name=S1\n*End Step\n'


def test_reads_plain_ascii(tmp_path):
	path = tmp_path / 'a.inp'
	path.write_bytes(_DECK.encode('ascii'))
	assert read_inp_text(str(path)) == _DECK


def test_reads_utf8_with_bom(tmp_path):
	"""The case that broke a real batch."""
	path = tmp_path / 'bom.inp'
	path.write_bytes(codecs.BOM_UTF8 + _DECK.encode('utf-8'))
	text = read_inp_text(str(path))
	assert text.startswith('*Heading'), "the BOM must be stripped"
	assert text == _DECK


def test_reads_utf8_non_ascii_comment(tmp_path):
	deck = '*Heading\n** 中文注释\n*Step\n*End Step\n'
	path = tmp_path / 'zh.inp'
	path.write_bytes(deck.encode('utf-8'))
	assert '中文注释' in read_inp_text(str(path))


def test_falls_back_without_raising_on_undecodable_bytes(tmp_path):
	"""latin-1 never fails, so a legacy-encoded deck still loads.

	Abaqus keywords are ASCII, so only comments and free text are affected —
	far better than refusing to run the job at all.
	"""
	path = tmp_path / 'legacy.inp'
	path.write_bytes(b'*Heading\n** \xb2\xe2\xca\xd4\n*Step\n*End Step\n')
	text = read_inp_text(str(path))
	assert text.startswith('*Heading')
	assert '*Step' in text


def test_crlf_is_preserved(tmp_path):
	path = tmp_path / 'crlf.inp'
	path.write_bytes(b'*Heading\r\n*Step\r\n*End Step\r\n')
	assert read_inp_text(str(path)) == '*Heading\r\n*Step\r\n*End Step\r\n'


def test_write_round_trips(tmp_path):
	path = tmp_path / 'out.inp'
	write_inp_text(str(path), _DECK)
	assert read_inp_text(str(path)) == _DECK


def test_write_does_not_translate_newlines(tmp_path):
	"""newline='' keeps whatever the caller produced."""
	path = tmp_path / 'out.inp'
	write_inp_text(str(path), '*Heading\r\n*Step\r\n')
	assert path.read_bytes() == b'*Heading\r\n*Step\r\n'


def test_write_then_read_non_ascii(tmp_path):
	path = tmp_path / 'zh.inp'
	write_inp_text(str(path), '*Heading\n** 中文\n')
	assert '中文' in read_inp_text(str(path))


def test_missing_file_raises(tmp_path):
	with pytest.raises(OSError):
		read_inp_text(str(tmp_path / 'absent.inp'))
