"""Parse textbook-placement lines into structured placements.

A placement line is one line under ``# Textbook``::

    Knight, Physics for Scientists and Engineers: A Strategic Approach
    with Modern Physics, 3rd Edition, Chapter 32, section 2

The 65 real lines in the corpus use at least seven grammars (audited
2026-07-07): lowercase ``chapter``, parenthetical chapter titles
(``Chapter 30 (Nuclear Physics)``), multi-sections (``Sections 1 & 5``),
bare topic tails (``Chapter 27, Nuclear Fission``), and shorthand
textbook names (``Knight, 2nd edition``) that the original pipeline's
exact-prefix matcher silently dropped — six real placements lost.

Contract here: NOTHING is dropped and nothing raises on real data.  The
verbatim line is always kept; an unrecognized textbook yields
``textbook_key=None`` plus a flag; unparseable tails become labels.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Canonical textbook keys -> full titles, as used in the corpus.
KNOWN_TEXTBOOKS = {
    "knight-calc-3rd": (
        "Knight, Physics for Scientists and Engineers: "
        "A Strategic Approach with Modern Physics, 3rd Edition"
    ),
    "knight-college-2nd": "Knight, College Physics: A Strategic Approach, 2nd Edition",
    "mandi-4th": "Matter and Interactions 4th Edition",
}

#: (alias prefix, canonical key, needs_review) — longest prefixes first.
#: needs_review marks aliases where the mapping is an informed guess the
#: importer should surface (e.g. "Knight, 2nd edition" almost certainly
#: means College Physics 2nd Ed per the original author's code comment,
#: but the entry gets a review flag rather than silent certainty).
ALIASES: list[tuple[str, str, bool]] = [
    (KNOWN_TEXTBOOKS["knight-calc-3rd"], "knight-calc-3rd", False),
    (KNOWN_TEXTBOOKS["knight-college-2nd"], "knight-college-2nd", False),
    (KNOWN_TEXTBOOKS["mandi-4th"], "mandi-4th", False),
    ("Matter and Interactions", "mandi-4th", False),
    ("Knight, Physics for Scientists and Engineers", "knight-calc-3rd", False),
    ("Knight, College Physics", "knight-college-2nd", False),
    ("Knight, 2nd edition", "knight-college-2nd", True),
    ("Knight, 3rd edition", "knight-calc-3rd", True),
]
# Longest alias first so full titles win over their own prefixes.
ALIASES.sort(key=lambda a: len(a[0]), reverse=True)

_CHAPTER_RE = re.compile(r"^chapters?\s+(\d+)\s*(.*)$", re.IGNORECASE)
_SECTION_RE = re.compile(r"^sections?\s+(.+)$", re.IGNORECASE)
_INT_LIST_RE = re.compile(r"\d+")


@dataclass
class Placement:
    raw_line: str
    textbook_key: str | None = None
    chapter: int | None = None
    sections: list[int] = field(default_factory=list)
    chapter_label: str | None = None  # e.g. "(Nuclear Physics)"
    extra_label: str | None = None  # e.g. "Nuclear Fission"
    flags: list[str] = field(default_factory=list)


def _match_textbook(line: str) -> tuple[str | None, str, list[str]]:
    """Return (key, remainder-after-title, flags) for the line."""
    folded = line.casefold()
    for alias, key, needs_review in ALIASES:
        if folded.startswith(alias.casefold()):
            flags = [f"textbook-alias-assumed:{alias}"] if needs_review else []
            return key, line[len(alias):], flags
    return None, line, ["unknown-textbook"]


def _parse_section_numbers(text: str) -> tuple[list[int], str | None]:
    """'1 & 5' -> [1, 5]; '2' -> [2]; non-numeric tails become a label."""
    numbers = [int(n) for n in _INT_LIST_RE.findall(text)]
    stripped = _INT_LIST_RE.sub("", text)
    leftover = stripped.strip(" ,&;-") or None
    if leftover and leftover.casefold() in {"and"}:
        leftover = None
    return numbers, leftover


def parse_placement(line: str) -> Placement:
    """Parse one placement line.  Never raises on corpus-shaped input."""
    placement = Placement(raw_line=line)
    key, remainder, flags = _match_textbook(line.strip())
    placement.textbook_key = key
    placement.flags.extend(flags)

    if key is None:
        # Unknown textbook: still try to pull chapter/section out of the
        # tail so the data is structured even while unassigned.
        remainder = line

    extra_parts: list[str] = []
    for part in remainder.split(","):
        part = part.strip()
        if not part:
            continue
        chapter_match = _CHAPTER_RE.match(part)
        if chapter_match:
            if placement.chapter is not None:
                # Never silently overwrite — the original pipeline's
                # signature failure mode.  The extra chapter stays in
                # the tail (and raw_line) and gets a review flag.
                placement.flags.append("multiple-chapters-on-one-line")
                extra_parts.append(part)
                continue
            placement.chapter = int(chapter_match.group(1))
            tail = chapter_match.group(2).strip()
            if tail:
                placement.chapter_label = tail
            continue
        section_match = _SECTION_RE.match(part)
        if section_match:
            numbers, leftover = _parse_section_numbers(section_match.group(1))
            placement.sections.extend(numbers)
            if leftover:
                extra_parts.append(leftover)
            continue
        # For unknown textbooks the pre-chapter text is the title itself,
        # not an "extra" — skip it (it survives verbatim in raw_line).
        if key is None and placement.chapter is None:
            continue
        extra_parts.append(part)

    if extra_parts:
        placement.extra_label = ", ".join(extra_parts)
        placement.flags.append("unparsed-tail")
    if key is not None and placement.chapter is None:
        placement.flags.append("no-chapter")
    return placement


def parse_placements(lines: list[str]) -> list[Placement]:
    """Parse every raw placement line of an entry.  len(out) == len(in)."""
    return [parse_placement(line) for line in lines]
