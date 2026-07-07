"""Placement-grammar acceptance tests: all 65 real lines parse or flag,
and the six placements the original matcher silently lost all surface."""

from nonnewtonian import parse_placement, parse_placements


def _all_placements(entries):
    out = {}
    for name, entry in entries.items():
        out[name] = parse_placements(entry.placements_raw)
    return out


def test_all_65_lines_yield_placements_zero_dropped(entries):
    placements = _all_placements(entries)
    total = sum(len(p) for p in placements.values())
    assert total == 65  # len(out) == len(in): nothing dropped, ever


def test_the_six_known_lost_placements_surface(entries):
    """The old startswith matcher silently dropped these six real lines.
    Here each either resolves via alias (flagged for review) or imports
    as unknown-textbook (flagged) — never disappears."""
    placements = _all_placements(entries)

    # 'Knight, 2nd edition/Edition' shorthand -> College Physics, flagged.
    wu = [p for p in placements["Chien-ShiungWu.txt"] if "2nd" in p.raw_line]
    assert wu and wu[0].textbook_key == "knight-college-2nd"
    assert any(f.startswith("textbook-alias-assumed") for f in wu[0].flags)
    assert wu[0].chapter == 30 and wu[0].sections == [5]
    assert wu[0].chapter_label  # '(Nuclear Physics)' survives as a label

    adesida = [p for p in placements["IlesanmiAdesida.txt"] if "2nd" in p.raw_line]
    assert adesida and adesida[0].textbook_key == "knight-college-2nd"
    assert adesida[0].chapter == 22 and adesida[0].sections == [1, 5]

    henry = [p for p in placements["WarrenHenry.txt"] if "2nd" in p.raw_line.lower()]
    assert henry and all(p.textbook_key == "knight-college-2nd" for p in henry)

    # Halliday lines (unknown textbook): kept, structured, flagged.
    yang = [p for p in placements["Chen-NingYang.txt"] if "Halliday" in p.raw_line]
    assert yang and yang[0].textbook_key is None
    assert "unknown-textbook" in yang[0].flags
    assert yang[0].chapter is not None

    murnane = [p for p in placements["MargaretMurnane.txt"] if "Halliday" in p.raw_line]
    assert murnane and murnane[0].textbook_key is None
    assert murnane[0].chapter == 6

    # MirandaCheng's Ginzberg line: unknown textbook, kept verbatim.
    cheng_unknown = [p for p in placements["MirandaCheng.txt"] if p.textbook_key is None]
    assert cheng_unknown


def test_emmy_noether_two_chapters(entries):
    """Multi-placement per scientist is by design (Ch9 and Ch10)."""
    noether = parse_placements(entries["EmmyNoether.txt"].placements_raw)
    chapters = sorted(p.chapter for p in noether if p.chapter)
    assert chapters == [9, 10]


def test_bare_topic_tail_becomes_label(entries):
    """'Chapter 27, Nuclear Fission' (E.C.G. Sudarshan's REAL line — the
    M1 review caught this test using a made-up calc-3rd variant): the
    old code just printed 'Unknown textbook part'; here it is kept as a
    label and the placement is flagged for review."""
    line = next(
        raw
        for raw in entries["ECGeorgeSudarshan.txt"].placements_raw
        if "Nuclear Fission" in raw
    )
    placement = parse_placement(line)
    assert placement.textbook_key == "knight-college-2nd"
    assert placement.chapter == 27
    assert placement.extra_label == "Nuclear Fission"
    assert "unparsed-tail" in placement.flags


def test_second_chapter_on_one_line_never_overwrites():
    """M1 review: a second 'Chapter N' part clobbered the first with no
    flag — the audited silent-drop class reincarnated.  Never again."""
    placement = parse_placement(
        "Matter and Interactions 4th Edition, Chapter 3, Chapter 11"
    )
    assert placement.chapter == 3  # first wins
    assert "multiple-chapters-on-one-line" in placement.flags
    assert "Chapter 11" in (placement.extra_label or "")


def test_int_crash_grammars_do_not_crash():
    """These exact strings raised ValueError in the original int() parse."""
    p1 = parse_placement("Knight, 2nd edition, Chapter 30 (Nuclear Physics), section 5")
    assert p1.chapter == 30 and p1.sections == [5]
    p2 = parse_placement("Knight, 2nd Edition, Chapter 22, Sections 1 & 5")
    assert p2.chapter == 22 and p2.sections == [1, 5]


def test_verbatim_raw_line_always_kept(entries):
    for name, entry in entries.items():
        for raw, placement in zip(entry.placements_raw, parse_placements(entry.placements_raw)):
            assert placement.raw_line == raw, name


def test_full_title_matches_do_not_get_review_flags():
    line = (
        "Knight, Physics for Scientists and Engineers: A Strategic Approach "
        "with Modern Physics, 3rd Edition, Chapter 9"
    )
    placement = parse_placement(line)
    assert placement.textbook_key == "knight-calc-3rd"
    assert not any(f.startswith("textbook-alias-assumed") for f in placement.flags)


def test_corpus_match_rates(entries):
    """Ground truth from the audit: 52 calc-3rd + 7 college-2nd exact
    matches, plus the 3 shorthand college-2nd lines the old code lost."""
    placements = [
        p
        for entry in entries.values()
        for p in parse_placements(entry.placements_raw)
    ]
    calc = sum(1 for p in placements if p.textbook_key == "knight-calc-3rd")
    college = sum(1 for p in placements if p.textbook_key == "knight-college-2nd")
    unknown = sum(1 for p in placements if p.textbook_key is None)
    assert calc == 52
    assert college == 10  # 7 exact + 3 recovered shorthand
    assert calc + college + unknown == 65
