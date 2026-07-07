"""Parse and round-trip the NonNewtonian scientist text-file format.

The format is the one used by mglerner/IntroductoryPhysics since 2016::

    # Name
    Margaret Murnane
    # Textbook
    Knight, ..., Chapter 32, section 2
    # Description
    Words words words...

    More words after a blank line.
    # Sources
    https://en.wikipedia.org/wiki/Margaret_Murnane
    # Photo
    https://example.edu/murnane.jpg

Real files vary (audited 2026-07-07 across the 39-file corpus), and this
parser must accept all of it without silently dropping anything:

- headers appear as ``# Name`` and ``#Name``, any case, with trailing
  markdown soft-break spaces;
- one file uses singular ``# Contributor`` (aliased to Contributors);
- the same header may repeat within a file (old format); repeated blocks
  are MERGED, never last-wins (the original pipeline lost data here);
- blocks appear in any order and any block except Name may be missing;
- body text is hard-wrapped at ~72 columns with blank lines separating
  real paragraphs, so prose blocks are unwrapped into paragraphs;
- unknown headers are preserved verbatim in ``Entry.extras``.

``parse(entry_to_text(e)) == e`` holds for every entry: ``entry_to_text``
emits the canonical form and parsing is a fixed point on it.  This is the
lossless-out guarantee — an entry can always leave the system as a plain
text file in the original format.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Headers with dedicated Entry fields.  Everything else lands in extras.
_KNOWN = {"name", "textbook", "description", "sources", "photo", "contributors"}
_ALIASES = {
    "contributor": "contributors",  # EmmyNoether.txt uses the singular
    "photos": "photo",
    "source": "sources",
    "textbooks": "textbook",
}
# Line-per-item blocks; the rest are prose and get paragraph-unwrapped.
_LINE_BLOCKS = {"name", "textbook", "photo", "contributors"}

_CANONICAL_TITLES = {
    "name": "Name",
    "textbook": "Textbook",
    "description": "Description",
    "sources": "Sources",
    "photo": "Photo",
    "contributors": "Contributors",
}
# Canonical emit order (matches the original ExampleScientist.txt).
_EMIT_ORDER = ["name", "textbook", "description", "sources", "photo", "contributors"]


class ParseError(ValueError):
    """Raised when a file cannot be parsed as a scientist entry at all."""


@dataclass
class Entry:
    """One scientist entry in canonical form.

    ``flags`` records parse events worth human review (merged repeated
    headers, extra lines under Name, ...).  It is deliberately excluded
    from equality so the round-trip guarantee compares content only:
    the canonical text has no repeated headers, so re-parsing it cannot
    reproduce the original file's flags.
    """

    name: str
    placements_raw: list[str] = field(default_factory=list)
    description: list[str] = field(default_factory=list)  # paragraphs
    sources: list[str] = field(default_factory=list)  # paragraphs
    photos: list[str] = field(default_factory=list)  # one URL/line each
    contributors: list[str] = field(default_factory=list)
    extras: dict[str, list[str]] = field(default_factory=dict)  # header -> paragraphs
    flags: list[str] = field(default_factory=list, compare=False)


def _normalize_header(raw_header: str) -> tuple[str, str]:
    """Return (canonical_key, display_title) for a ``#``-prefixed line."""
    title = raw_header.lstrip("#").strip()
    key = title.casefold()
    key = _ALIASES.get(key, key)
    if key in _KNOWN:
        return key, _CANONICAL_TITLES[key]
    return key, title


def iter_blocks(text: str):
    """Yield (raw_header_line, [body lines]) in file order.

    Lines before the first header are yielded under a None header so
    callers can decide what to do with stray preamble text.
    """
    header = None
    body: list[str] = []
    for line in text.split("\n"):
        if line.startswith("#"):
            if header is not None or body:
                yield header, body
            header, body = line, []
        else:
            body.append(line)
    if header is not None or body:
        yield header, body


def _looks_like_url(token: str) -> bool:
    return "://" in token or "www." in token


def _join_wrapped(pieces: list[str], join_events: list[str] | None = None) -> str:
    """Join stripped lines of one paragraph with spaces — except when a
    line broke inside a token.  The corpus (audited, then re-audited by
    the M1 adversarial review) wraps mid-token in three ways, all of
    which a plain space-join corrupts:

    - URLs wrapped at a hyphen (``...pioneers-`` / ``technology-...``),
      including scheme-less markdown-link text (``[www.pbs.org/...-``);
    - URLs wrapped at a slash (``.../`` / ``mad/physics/henry.html``) —
      but two *separate* URLs on adjacent lines must stay separate;
    - prose hyphenation (``two-`` / ``dimensional``), joined only when
      the next line starts lowercase, and reported via ``join_events``
      so callers can flag it for human review rather than guess silently.
    """
    text = ""
    for piece in pieces:
        if not text:
            text = piece
            continue
        last_token = text.rsplit(None, 1)[-1]
        first_token = piece.split(None, 1)[0]
        if _looks_like_url(last_token) and last_token.endswith("-"):
            text += piece
        elif (
            _looks_like_url(last_token)
            and last_token.endswith("/")
            and "/" in first_token
            and not _looks_like_url(first_token)
        ):
            text += piece
        elif last_token.endswith("-") and not _looks_like_url(last_token) and piece[:1].islower():
            if join_events is not None:
                join_events.append(last_token + first_token)
            text += piece
        else:
            text += " " + piece
    return text


def unwrap_paragraphs(lines: list[str], join_events: list[str] | None = None) -> list[str]:
    """Join hard-wrapped lines into paragraphs; blank lines separate them.

    Trailing whitespace (including markdown two-space soft breaks) is
    stripped per line before joining.  ``join_events`` (if given)
    collects prose words rejoined across a hyphen line break, so callers
    can surface them for review.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped:
            current.append(stripped)
        elif current:
            paragraphs.append(_join_wrapped(current, join_events))
            current = []
    if current:
        paragraphs.append(_join_wrapped(current, join_events))
    return paragraphs


def _nonempty_lines(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _unescape_line(line: str) -> str:
    r"""Reverse entry_to_text's escaping: a body line emitted as ``\#...``
    was a content line that starts with ``#``, not a header."""
    return line[1:] if line.startswith("\\#") else line


def parse(text: str) -> Entry:
    """Parse one scientist file's text into an Entry.

    Never drops content: repeated blocks merge, unknown headers go to
    extras, and anything surprising adds a flag instead of vanishing.
    """
    blocks: dict[str, list[str]] = {}
    display_titles: dict[str, str] = {}
    flags: list[str] = []

    for raw_header, body in iter_blocks(text):
        if raw_header is None:
            if _nonempty_lines(body):
                blocks.setdefault("_preamble", []).extend(body)
                flags.append("text-before-first-header")
            continue
        key, title = _normalize_header(raw_header)
        display_titles.setdefault(key, title)
        if key in blocks:
            # Old-format files repeat headers (sometimes with empty
            # bodies); merging is silent data loss territory in the
            # original pipeline, so any repeat is flagged for review.
            flags.append(f"merged-repeated-header:{title}")
            # Blank separator keeps paragraph boundaries between merged blocks.
            blocks[key].append("")
        blocks.setdefault(key, []).extend(_unescape_line(line) for line in body)

    name_lines = _nonempty_lines(blocks.pop("name", []))
    if not name_lines:
        raise ParseError(
            "no '# Name' block found; headers present: "
            + ", ".join(sorted(display_titles.values()))
        )
    if len(name_lines) > 1:
        flags.append("extra-lines-under-name")
    name = name_lines[0]

    join_events: list[str] = []
    entry = Entry(
        name=name,
        placements_raw=_nonempty_lines(blocks.pop("textbook", [])),
        description=unwrap_paragraphs(blocks.pop("description", []), join_events),
        sources=unwrap_paragraphs(blocks.pop("sources", []), join_events),
        photos=_nonempty_lines(blocks.pop("photo", [])),
        contributors=_nonempty_lines(blocks.pop("contributors", [])),
        flags=flags,
    )
    # Extra name lines are preserved, not dropped.
    if len(name_lines) > 1:
        entry.extras["Name notes"] = name_lines[1:]
    preamble = blocks.pop("_preamble", None)
    if preamble:
        entry.extras["Preamble"] = unwrap_paragraphs(preamble)
    for key, body in blocks.items():
        content = unwrap_paragraphs(body, join_events)
        if content:
            entry.extras[display_titles[key]] = content
    # Prose hyphen-joins are a judgment call: surface them for review.
    entry.flags.extend(f"hyphen-wrap-joined:{word}" for word in join_events)
    return entry


def parse_file(path) -> Entry:
    """Parse a scientist file from disk (UTF-8, BOM-tolerant, NFC)."""
    import unicodedata

    # utf-8-sig: a leading BOM would otherwise hide '# Name' and produce
    # a baffling "no Name block" error (M1 review finding).
    with open(path, encoding="utf-8-sig") as handle:
        return parse(unicodedata.normalize("NFC", handle.read()))


def _escape_line(line: str) -> str:
    r"""Content lines starting with ``#`` would be re-read as headers;
    emit them as ``\#...`` (parse unescapes).  Without this, a paragraph
    like '#1 ranked physicist' silently vanished on round-trip (M1
    adversarial review finding)."""
    return "\\" + line if line.startswith("#") else line


def _validate_items(kind: str, items: list[str]) -> None:
    for item in items:
        if "\n" in item:
            raise ValueError(
                f"{kind} contains an embedded newline: {item!r}. "
                "Split it into separate items/paragraphs instead — a "
                "silent rewrite here would break the round-trip contract."
            )


def entry_to_text(entry: Entry) -> str:
    """Emit the canonical plain-text form of an entry.

    ``parse(entry_to_text(e)) == e`` (flags excluded) for every entry
    this function accepts.  It rejects — with a clear ValueError, never
    a silent rewrite — entries that cannot survive the text format:
    items/paragraphs with embedded newlines, and extras keys that
    collide with a standard header (e.g. ``extras={'Photos': ...}``
    would fold into the photos field on re-parse).
    """
    _validate_items("name", [entry.name])
    for kind, items in (
        ("placements_raw", entry.placements_raw),
        ("description", entry.description),
        ("sources", entry.sources),
        ("photos", entry.photos),
        ("contributors", entry.contributors),
    ):
        _validate_items(kind, items)
    for title, paragraphs in entry.extras.items():
        key = _ALIASES.get(title.casefold(), title.casefold())
        if key in _KNOWN or key == "name":
            raise ValueError(
                f"extras key {title!r} collides with the standard "
                f"{_CANONICAL_TITLES[key]!r} header; put that content in "
                "the corresponding Entry field instead."
            )
        _validate_items(f"extras[{title!r}]", paragraphs)

    chunks: list[str] = []

    def block(title: str, paragraphs: list[str]) -> None:
        if paragraphs:
            escaped = ["\n".join(_escape_line(l) for l in p.split("\n")) for p in paragraphs]
            chunks.append(f"# {title}\n" + "\n\n".join(escaped))

    def line_block(title: str, items: list[str]) -> None:
        if items:
            chunks.append(f"# {title}\n" + "\n".join(_escape_line(i) for i in items))

    line_block("Name", [entry.name])
    line_block("Textbook", entry.placements_raw)
    block("Description", entry.description)
    block("Sources", entry.sources)
    line_block("Photo", entry.photos)
    line_block("Contributors", entry.contributors)
    for title, paragraphs in entry.extras.items():
        block(title, paragraphs)
    return "\n\n".join(chunks) + "\n"
