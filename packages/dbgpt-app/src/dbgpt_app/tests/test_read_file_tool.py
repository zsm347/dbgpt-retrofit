"""Tests for read_file output caps and limit bounds (P1-23)."""

import json
import re

from dbgpt_app.openapi.api_v1.tools.read_file import (
    _MAX_OUTPUT_CHARS,
    make_read_file,
)


def _write_persisted(tmp_path, name, content):
    directory = tmp_path / "persisted_results"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def _content(result):
    return json.loads(result)["chunks"][0]["content"]


def _continue_offset(text):
    match = re.search(r"use offset=(\d+) to continue", text)
    assert match, f"continuation hint missing near: {text[-200:]!r}"
    return int(match.group(1))


def test_small_reads_keep_the_exact_legacy_format(tmp_path):
    path = _write_persisted(tmp_path, "small.txt", "hello\nworld\n")
    read_file = make_read_file({"output_dir": str(tmp_path)})

    text = _content(read_file(file_path=path, offset=0, limit=100))

    assert text == (
        f"File: {path} (2 lines, showing 1-2)\n"
        "     1 | hello\n"
        "     2 | world"
    )


def test_large_read_is_bounded_and_continuable(tmp_path):
    line_body = "x" * 70
    total_lines = 3000
    content = "".join(f"line-{i:04d}-{line_body}\n" for i in range(total_lines))
    path = _write_persisted(tmp_path, "big.txt", content)
    read_file = make_read_file({"output_dir": str(tmp_path)})

    first = _content(read_file(file_path=path, offset=0, limit=10_000))
    assert len(first) <= _MAX_OUTPUT_CHARS
    offset = _continue_offset(first)
    assert 0 < offset < total_lines

    second = _content(read_file(file_path=path, offset=offset, limit=10_000))
    assert len(second) <= _MAX_OUTPUT_CHARS
    assert _continue_offset(second) > offset


def test_limit_is_capped_at_two_thousand_lines(tmp_path):
    content = "".join(f"row-{i:04d}\n" for i in range(3000))
    path = _write_persisted(tmp_path, "rows.txt", content)
    read_file = make_read_file({"output_dir": str(tmp_path)})

    text = _content(read_file(file_path=path, offset=0, limit=10_000_000))

    assert "showing 1-2000" in text
    assert "use offset=2000 to continue" in text


def test_giant_first_line_is_capped_and_next_lines_reachable(tmp_path):
    content = "y" * 300_000 + "\nafter\n" + "tail\n"
    path = _write_persisted(tmp_path, "giant.txt", content)
    read_file = make_read_file({"output_dir": str(tmp_path)})

    first = _content(read_file(file_path=path, offset=0, limit=100))
    assert len(first) <= _MAX_OUTPUT_CHARS
    assert "use offset=1 to continue" in first

    second = _content(read_file(file_path=path, offset=1, limit=100))
    assert "after" in second
    assert "tail" in second
