"""Read helpers that assemble display objects from the database.

These turn DB rows back into the shapes the templates and the slide
generator want: an entry with its paragraphs, placements, and photos;
a textbook with its chapters, each carrying the approved entries placed
there.  Only APPROVED, communal-APPROVED entries are ever returned by
the public helpers — pending/rejected content never leaks to a page.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from ..parser import Entry


@dataclass
class DisplayPhoto:
    file_path: str | None
    original_url: str | None
    attribution: str | None
    license: str | None
    license_verified: bool
    is_primary: bool
    recovered: bool


@dataclass
class DisplayPlacement:
    textbook_slug: str | None
    textbook_title: str | None
    chapter: int | None
    section_label: str | None
    raw_line: str


@dataclass
class DisplayEntry:
    id: int
    name: str
    slug: str
    description: list[str]          # paragraphs
    sources: list[str]             # paragraphs
    contributor_name: str | None
    attribution_mode: str
    why_chapter: str | None
    wikipedia_url: str | None
    license_notice: str | None
    is_seed: bool
    photos: list[DisplayPhoto] = field(default_factory=list)
    placements: list[DisplayPlacement] = field(default_factory=list)

    @property
    def primary_photo(self) -> DisplayPhoto | None:
        stored = [p for p in self.photos if p.file_path]
        if not stored:
            return None
        return next((p for p in stored if p.is_primary), stored[0])

    def displayed_credit(self) -> str | None:
        """Apply the attribution mode to the contributor name."""
        if not self.contributor_name or self.attribution_mode == "anonymous":
            return None
        if self.attribution_mode == "first_initial":
            parts = self.contributor_name.split()
            if len(parts) >= 2:
                return f"{parts[0]} {parts[-1][0]}."
            return parts[0] if parts else None
        return self.contributor_name  # 'full'

    def to_parser_entry(self) -> Entry:
        """Rebuild a parser.Entry for slide generation."""
        return Entry(
            name=self.name,
            placements_raw=[p.raw_line for p in self.placements],
            description=self.description,
            sources=self.sources,
            photos=[p.original_url for p in self.photos if p.original_url],
            contributors=[self.contributor_name] if self.contributor_name else [],
        )


def _split_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in (text or "").split("\n\n") if p.strip()]


def safe_http_url(url: str | None) -> str | None:
    """Return the URL only if it's a plain http(s) link, else None.

    Jinja autoescaping neutralizes HTML metacharacters but NOT dangerous
    URL schemes, so a stored ``javascript:``/``data:`` value would become
    live XSS the moment it lands in an href (M3 review, confirmed
    critical).  Every href built from stored/user URLs must pass through
    here.  Guarded at the display layer so M4 student-submitted photo
    URLs are covered by the same gate."""
    if not url:
        return None
    stripped = url.strip()
    low = stripped.lower()
    if low.startswith("http://") or low.startswith("https://"):
        return stripped
    return None


def _row_to_entry(conn, row) -> DisplayEntry:
    entry = DisplayEntry(
        id=row["id"],
        name=row["scientist_name"],
        slug=row["scientist_slug"],
        description=_split_paragraphs(row["description"]),
        sources=_split_paragraphs(row["sources_text"]),
        contributor_name=row["contributor_name"],
        attribution_mode=row["attribution_mode"],
        why_chapter=row["why_chapter"],
        wikipedia_url=row["wikipedia_url"],
        license_notice=row["license_notice"],
        is_seed=row["seed_origin"] is not None,
    )
    for p in conn.execute(
        "SELECT ph.*, NULL AS x FROM photos ph WHERE ph.entry_id=? ORDER BY is_primary DESC, id",
        (row["id"],),
    ):
        entry.photos.append(DisplayPhoto(
            file_path=p["file_path"], original_url=safe_http_url(p["original_url"]),
            attribution=p["attribution"], license=p["license"],
            license_verified=bool(p["license_verified"]),
            is_primary=bool(p["is_primary"]),
            recovered=p["fetch_status"] == "recovered",
        ))
    for p in conn.execute(
        "SELECT pl.*, t.slug AS tb_slug, t.title AS tb_title "
        "FROM placements pl LEFT JOIN textbooks t ON pl.textbook_id=t.id "
        "WHERE pl.entry_id=? ORDER BY pl.chapter",
        (row["id"],),
    ):
        entry.placements.append(DisplayPlacement(
            textbook_slug=p["tb_slug"], textbook_title=p["tb_title"],
            chapter=p["chapter"], section_label=p["section_label"],
            raw_line=p["raw_line"],
        ))
    return entry


# --- public: approved communal content only -------------------------------

_PUBLIC_WHERE = (
    "WHERE collection_id IS NULL AND status='approved' AND communal_status='approved'"
)


def approved_entry_by_id(conn, entry_id: int) -> DisplayEntry | None:
    row = conn.execute(
        f"SELECT * FROM entries {_PUBLIC_WHERE} AND id=?", (entry_id,)
    ).fetchone()
    return _row_to_entry(conn, row) if row else None


def approved_entries_by_slug(conn, slug: str) -> list[DisplayEntry]:
    """All approved writeups for one scientist (there can be several)."""
    rows = conn.execute(
        f"SELECT * FROM entries {_PUBLIC_WHERE} AND scientist_slug=? ORDER BY id", (slug,)
    ).fetchall()
    return [_row_to_entry(conn, r) for r in rows]


def all_approved_scientists(conn, search: str | None = None) -> list[DisplayEntry]:
    """One display entry per distinct scientist (the first approved
    writeup), for the communal index; optional case-insensitive search."""
    sql = f"SELECT * FROM entries {_PUBLIC_WHERE}"
    params: list = []
    if search:
        sql += " AND scientist_name LIKE ? "
        params.append(f"%{search}%")
    sql += " ORDER BY scientist_name COLLATE NOCASE, id"  # id tie-breaks: stable primary writeup
    seen: set[str] = set()
    out: list[DisplayEntry] = []
    for row in conn.execute(sql, params):
        if row["scientist_slug"] in seen:
            continue
        seen.add(row["scientist_slug"])
        out.append(_row_to_entry(conn, row))
    return out


def list_textbooks(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM textbooks ORDER BY title COLLATE NOCASE")]


def public_textbooks(conn) -> list[dict]:
    """Textbooks to show on the public site: the builtin ones, plus any
    custom (per-class) textbook that has actually accrued approved
    communal content — so a teacher's private textbook name doesn't
    surface publicly until a shared entry lands on it (M5)."""
    return [dict(r) for r in conn.execute(
        "SELECT t.* FROM textbooks t WHERE t.is_builtin=1 OR EXISTS ("
        "  SELECT 1 FROM entries e JOIN placements p ON p.entry_id=e.id "
        "  WHERE p.textbook_id=t.id AND e.collection_id IS NULL "
        "  AND e.status='approved' AND e.communal_status='approved') "
        "ORDER BY t.title COLLATE NOCASE")]


def textbook_by_slug(conn, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM textbooks WHERE slug=?", (slug,)).fetchone()
    return dict(row) if row else None


@dataclass
class ChapterBlock:
    chapter: int
    topics: str
    entries: list[DisplayEntry] = field(default_factory=list)


def textbook_chapters(conn, textbook_id: int) -> list[ChapterBlock]:
    """Every chapter of the textbook (gaps included, deliberately), each
    with the approved entries placed there.  This is the flagship
    browse view: readable in full without downloading anything."""
    chapters: list[ChapterBlock] = []
    by_chapter: dict[int, ChapterBlock] = {}
    for row in conn.execute(
        "SELECT DISTINCT chapter, topics FROM toc_rows WHERE textbook_id=? ORDER BY sort_order",
        (textbook_id,),
    ):
        block = ChapterBlock(chapter=row["chapter"], topics=row["topics"])
        chapters.append(block)
        by_chapter[row["chapter"]] = block

    seen_per_chapter: dict[int, set[str]] = {c.chapter: set() for c in chapters}
    for row in conn.execute(
        "SELECT e.*, pl.chapter AS placed_chapter FROM entries e "
        "JOIN placements pl ON pl.entry_id=e.id "
        "WHERE e.collection_id IS NULL AND e.status='approved' "
        "AND e.communal_status='approved' AND pl.textbook_id=? "
        "ORDER BY e.scientist_name COLLATE NOCASE",
        (textbook_id,),
    ):
        block = by_chapter.get(row["placed_chapter"])
        if block is None:
            continue
        if row["scientist_slug"] in seen_per_chapter[block.chapter]:
            continue
        seen_per_chapter[block.chapter].add(row["scientist_slug"])
        block.entries.append(_row_to_entry(conn, row))
    return chapters


def wanted_scientists(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM wanted_scientists ORDER BY name COLLATE NOCASE")]


# --- class-collection views (scoped to one collection) --------------------

def class_chapters(conn, collection_id: int, textbook_id: int) -> list[ChapterBlock]:
    """Chapter-indexed view for one class: every chapter of the class's
    textbook, each with this collection's APPROVED entries placed there."""
    chapters: list[ChapterBlock] = []
    by_chapter: dict[int, ChapterBlock] = {}
    for row in conn.execute(
        "SELECT DISTINCT chapter, topics FROM toc_rows WHERE textbook_id=? ORDER BY sort_order",
        (textbook_id,),
    ):
        block = ChapterBlock(chapter=row["chapter"], topics=row["topics"])
        chapters.append(block)
        by_chapter[row["chapter"]] = block
    seen = {c.chapter: set() for c in chapters}
    for row in conn.execute(
        "SELECT e.*, pl.chapter AS placed_chapter FROM entries e "
        "JOIN placements pl ON pl.entry_id=e.id "
        "WHERE e.collection_id=? AND e.status='approved' AND pl.textbook_id=? "
        "ORDER BY e.scientist_name COLLATE NOCASE",
        (collection_id, textbook_id),
    ):
        block = by_chapter.get(row["placed_chapter"])
        if block is None or row["scientist_slug"] in seen[block.chapter]:
            continue
        seen[block.chapter].add(row["scientist_slug"])
        block.entries.append(_row_to_entry(conn, row))
    return chapters


def approved_class_entry(conn, collection_id: int, entry_id: int) -> DisplayEntry | None:
    row = conn.execute(
        "SELECT * FROM entries WHERE id=? AND collection_id=? AND status='approved'",
        (entry_id, collection_id)).fetchone()
    return _row_to_entry(conn, row) if row else None

