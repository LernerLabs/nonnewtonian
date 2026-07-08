"""The TOML source-of-truth format must be a LOSSLESS replacement for a
textbook's manifest.json entry + its .csv TOC. If this holds, the migration
away from CSV+JSON can be done mechanically."""
import json
import pathlib

import pytest

from nonnewtonian.textbook_toml import TextbookTomlError, load_textbook_toml
from nonnewtonian.toc import load_toc_file

DATA = pathlib.Path(__file__).resolve().parents[1] / "data/textbooks"


def _manifest_entry(slug):
    manifest = json.loads((DATA / "manifest.json").read_text(encoding="utf-8"))
    return next(t for t in manifest["textbooks"] if t["slug"] == slug)


def test_toml_matches_manifest_and_csv_exactly():
    slug = "knight-calc-3rd"
    tb = load_textbook_toml(DATA / f"{slug}.toml")
    entry = _manifest_entry(slug)

    # metadata
    assert tb.slug == entry["slug"]
    assert tb.title == entry["title"]
    assert tb.author == entry["author"]
    assert tb.edition == entry["edition"]
    assert tb.discipline == entry["discipline"]

    # aliases (same shape the importer already consumes)
    assert tb.aliases == entry["aliases"]

    # table of contents: identical TocRows, in order, to the CSV
    assert tb.toc == load_toc_file(DATA / f"{slug}.csv")


def test_missing_required_field_is_a_clear_error(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text('title = "no slug here"\n', encoding="utf-8")
    with pytest.raises(TextbookTomlError, match="missing required 'slug'"):
        load_textbook_toml(bad)


def test_decreasing_chapters_rejected(tmp_path):
    bad = tmp_path / "bad.toml"
    bad.write_text(
        'slug = "x"\ntitle = "X"\n'
        "[[chapter]]\nnumber = 5\ntopics = \"a\"\n"
        "[[chapter]]\nnumber = 2\ntopics = \"b\"\n",
        encoding="utf-8",
    )
    with pytest.raises(TextbookTomlError, match="must not decrease"):
        load_textbook_toml(bad)
