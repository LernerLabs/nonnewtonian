"""TOC validation: the Knight CSV loads verbatim; the corrupted M&I CSV
is rejected loudly and loads 23 chapters after repair."""

import pytest

from nonnewtonian import TocError, load_toc, load_toc_file, repair_doubled_quotes

from conftest import FIXTURES


def test_knight_csv_loads_42_chapters():
    rows = load_toc_file(FIXTURES / "Knight3rdEdition.csv")
    assert [row.chapter for row in rows] == list(range(1, 43))
    assert all(row.section is None for row in rows)  # 100% blank, audited
    assert rows[2].topics == "Vectors"


def test_broken_mandi_csv_is_rejected_readably():
    """The original file's doubled quotes fold 23 chapters into 13
    records; the old pipeline would have published that silently."""
    text = (FIXTURES / "MandI4thEdition.csv").read_text()
    with pytest.raises(TocError) as excinfo:
        load_toc(text)
    # The message is for teachers, not tracebacks.
    assert "line break" in str(excinfo.value) or "quoting" in str(excinfo.value)


def test_repaired_mandi_csv_loads_23_chapters():
    text = (FIXTURES / "MandI4thEdition.csv").read_text()
    rows = load_toc(repair_doubled_quotes(text))
    assert [row.chapter for row in rows] == list(range(1, 24))
    assert rows[0].topics == "Interactions and Motion"
    assert rows[1].topics == "The Momentum Principle"  # the swallowed chapter


def test_wrong_header_rejected():
    with pytest.raises(TocError, match="Chapter,Section,Topics"):
        load_toc("Ch,Sec,Top\n1,,Stuff\n")


def test_non_numeric_chapter_rejected():
    with pytest.raises(TocError, match="whole number"):
        load_toc('Chapter,Section,Topics\none,,"Motion"\n')


def test_decreasing_chapters_rejected():
    with pytest.raises(TocError, match="must not decrease"):
        load_toc('Chapter,Section,Topics\n2,,"B"\n1,,"A"\n')


def test_blank_lines_skipped():
    rows = load_toc('Chapter,Section,Topics\n1,,"A"\n\n2,,"B"\n')
    assert len(rows) == 2


def test_sections_load_when_present():
    rows = load_toc('Chapter,Section,Topics\n1,1,"A"\n1,2,"B"\n2,,"C"\n2,1,"D"\n')
    assert [(r.chapter, r.section) for r in rows] == [(1, 1), (1, 2), (2, None), (2, 1)]


def test_repair_leaves_legitimately_empty_quoted_field_alone():
    """M1 review: the repair must not corrupt a valid '1,,""' line."""
    text = 'Chapter,Section,Topics\n1,,""\n2,,"Real topic""\n'
    repaired = repair_doubled_quotes(text)
    assert '1,,""' in repaired  # untouched
    assert '2,,"Real topic"' in repaired  # fixed
