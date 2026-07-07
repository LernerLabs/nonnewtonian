"""Acceptance tests over the 39 real scientist files, plus the specific
bug classes the 2026-07-07 audit found in the original parser."""

from nonnewtonian import entry_to_text, parse
from nonnewtonian.parser import unwrap_paragraphs


def test_all_39_files_parse(entries):
    assert len(entries) == 39
    for name, entry in entries.items():
        assert entry.name, name


def test_corpus_field_counts(entries):
    """Counts verified by hand against the corpus on 2026-07-07."""
    assert sum(1 for e in entries.values() if e.photos) == 38
    assert sum(1 for e in entries.values() if e.sources) == 36
    assert sum(len(e.placements_raw) for e in entries.values()) == 65


def test_corpus_content_volume_pinned(entries):
    """Presence counts alone can't catch parse-side truncation (a
    mutation test showed sources[:1] passing everything), so paragraph
    VOLUME is pinned too.  Round-trip is structurally blind to losses
    that happen during parse itself."""
    assert sum(len(e.description) for e in entries.values()) == 51
    assert sum(len(e.sources) for e in entries.values()) == 44


def test_ursula_franklin_has_no_photo(entries):
    assert entries["UrsulaFranklin.txt"].photos == []


def test_no_space_headers_parse(entries):
    """NergisMavalvala.txt uses '#Name' style headers throughout."""
    entry = entries["NergisMavalvala.txt"]
    assert entry.name == "Nergis Mavalvala"
    assert entry.description


def test_singular_contributor_header_aliased(entries):
    """EmmyNoether.txt is the one file using '# Contributor'."""
    assert entries["EmmyNoether.txt"].contributors


def test_repeated_textbook_headers_merge(entries):
    """HadiyahGreen.txt has three '# Textbook' headers (old format);
    the original code's accumulation bug dropped repeated prose blocks."""
    entry = entries["HadiyahGreen.txt"]
    assert entry.placements_raw  # merged, not lost
    assert any(flag.startswith("merged-repeated-header") for flag in entry.flags)


def test_out_of_order_sections(entries):
    """SubrahmanyanChandrasekhar.txt puts Photo before Description."""
    entry = entries["SubrahmanyanChandrasekhar.txt"]
    assert entry.photos and entry.description


def test_repeated_description_blocks_are_merged_not_last_wins():
    """The original pipeline rendered only the LAST repeated block —
    silent loss of student prose (makesyllabus.py:136-143)."""
    text = "# Name\nA Scientist\n# Description\nFirst block.\n# Description\nSecond block.\n"
    entry = parse(text)
    joined = " ".join(entry.description)
    assert "First block." in joined and "Second block." in joined


def test_unknown_headers_preserved_in_extras():
    text = "# Name\nA Scientist\n# Quotes\nSomething memorable.\n"
    entry = parse(text)
    assert entry.extras["Quotes"] == ["Something memorable."]


def test_paragraph_unwrap_and_soft_breaks():
    lines = ["First line  ", "second line.", "", "New paragraph."]
    assert unwrap_paragraphs(lines) == ["First line second line.", "New paragraph."]


def test_hyphen_split_url_rejoined():
    """Three corpus files hard-wrap URLs at a hyphen; joining with a
    space corrupts the link (audit: HadiyahGreen, Tomonaga, WarrenHenry)."""
    lines = ["https://example.org/pioneers-", "technology-article"]
    assert unwrap_paragraphs(lines) == ["https://example.org/pioneers-technology-article"]


def test_wrapped_tokens_rejoined_in_real_files(entries):
    """The M1 adversarial review found the first join heuristic silently
    corrupted three real files.  All three, pinned forever:"""
    # Shirley-Ann-Jackson: 'two-\ndimensional' prose hyphenation.
    jackson = entries["Shirley-Ann-Jackson.txt"]
    assert any("two-dimensional systems" in p for p in jackson.description)
    assert "hyphen-wrap-joined:two-dimensional" in jackson.flags  # surfaced, not silent
    # WarrenHenry: URL wrapped at a slash.
    henry = entries["WarrenHenry.txt"]
    assert any("math.buffalo.edu/mad/physics/henry_warren.html" in p for p in henry.sources)
    # SylvesterJamesGate: scheme-less markdown link text wrapped at a hyphen.
    gates = entries["SylvesterJamesGate.txt"]
    assert any("view-gates.html](" in p for p in gates.sources)


def test_adjacent_standalone_urls_stay_separate(entries):
    """JeevakParpia lists two URLs on adjacent lines; the slash-join
    rule must not glue them into one."""
    parpia = entries["JeevakParpia.txt"]
    text = " ".join(parpia.sources)
    assert "parpia.lassp.cornell.edu/ http://www.news.cornell.edu" in text


def test_round_trip_all_39(entries):
    """The lossless-out guarantee: parse(entry_to_text(e)) == e."""
    for name, entry in entries.items():
        assert parse(entry_to_text(entry)) == entry, name


def test_round_trip_is_fixed_point(entries):
    for name, entry in entries.items():
        once = entry_to_text(entry)
        assert entry_to_text(parse(once)) == once, name


def test_round_trip_hash_leading_paragraph():
    """M1 review: '#1 ranked physicist' silently VANISHED on round-trip
    (re-read as a bogus empty header).  Now escaped on emit."""
    from nonnewtonian import Entry

    entry = Entry(name="X Y", description=["#1 ranked physicist in her class"])
    assert parse(entry_to_text(entry)) == entry


def test_entry_to_text_rejects_embedded_newlines():
    """A silent rewrite would break round-trip; reject loudly instead."""
    import pytest

    from nonnewtonian import Entry

    with pytest.raises(ValueError, match="newline"):
        entry_to_text(Entry(name="X", description=["line one\nline two"]))


def test_entry_to_text_rejects_colliding_extras_keys():
    """extras={'Photos': ...} would fold into the photos field on
    re-parse — reject at emit time."""
    import pytest

    from nonnewtonian import Entry

    with pytest.raises(ValueError, match="collides"):
        entry_to_text(Entry(name="X", extras={"Photos": ["http://x/y.jpg"]}))


def test_round_trip_off_corpus_paths():
    """Preamble text, extra Name lines, and unknown headers round-trip;
    a mutation run showed deleting the extras emission passed 45/45."""
    text = (
        "stray preamble line\n"
        "# Name\nA Scientist\nSecond name line\n"
        "# Quotes\nSomething memorable.\n"
    )
    entry = parse(text)
    assert entry.extras["Preamble"] == ["stray preamble line"]
    assert entry.extras["Name notes"] == ["Second name line"]
    assert entry.extras["Quotes"] == ["Something memorable."]
    emitted = entry_to_text(entry)
    for header in ("# Preamble", "# Name notes", "# Quotes"):
        assert header in emitted
    assert parse(emitted) == entry


def test_bom_tolerated(tmp_path):
    """A UTF-8 BOM must not hide '# Name' (utf-8-sig read)."""
    from nonnewtonian import parse_file

    path = tmp_path / "bom.txt"
    path.write_bytes(b"\xef\xbb\xbf# Name\nSomeone\n")
    assert parse_file(path).name == "Someone"
