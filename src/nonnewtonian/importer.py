"""Seed importer: load the 39 original scientist files + TOC CSVs into
the SQLite database as communal-pending entries.

Contract (from the plan and its adversarial review):
  * nothing is silently dropped — unmatched placements are kept with
    textbook_id NULL and a flag; repeated blocks were already merged by
    the parser; corrections are applied with the original preserved.
  * nothing is silently published — every seed entry lands
    status='pending', communal_status='pending' for human review.
  * dry-run prints a per-file report and writes nothing.
  * re-running is idempotent (prior seed rows are cleared first).

Seed policy decisions (Michael, 2026-07-07):
  * contributor names kept as written in the files;
  * photos published with a 'source unverified - origin link only'
    marker where not positively licensed (all local until deploy);
  * Wikipedia-derived prose carries a CC BY-SA notice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import photos as photos_mod
from .parser import Entry, parse_file
from .placements import parse_placements
from .slugs import slugify
from .toc import load_toc_file

# --- correction / flag data, from the 2026-07-07 data-migration audit ---

# Applied at seed time (these become public page headings); original
# always preserved via a review flag recording what changed.
NAME_CORRECTIONS = {
    "Michio Katu": "Michio Kaku",
    "Sylvester James Gate": "Sylvester James Gates",
}

# Flagged for human review but NOT auto-edited (prose corrections are
# risky; a human confirms in the moderation queue).
REVIEW_NOTES = {
    "CVRaman.txt": "description says 'born ... 1988' — likely 1888 (verify)",
    "MirandaCheng.txt": "placement cites author 'Ginzberg' — likely Ginzburg (verify)",
}

# Descriptions that are near-verbatim from a share-alike/quoted source.
WIKIPEDIA_VERBATIM = {
    "LiseMeitner.txt", "LeneHau.txt", "FabiolaGianotti.txt",
    "UrsulaFranklin.txt", "SandraFaber.txt",
}
QUOTED_SOURCE = {"DeeptoChakrabarty.txt", "RaymondAshoori.txt"}

MULTI_STUDENT_MARKER = "[Commentary from a second student:]"
_FOOTNOTE_MARKER = re.compile(r"\[\d+\]")


@dataclass
class FileReport:
    filename: str
    name: str
    fields_present: list[str] = field(default_factory=list)
    fields_missing: list[str] = field(default_factory=list)
    entries_created: int = 0
    placements_total: int = 0
    placements_matched: int = 0
    placements_unassigned: int = 0
    photos_total: int = 0
    photos_stored: int = 0
    photos_recovered: int = 0
    photos_dead: int = 0
    corrections: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


@dataclass
class SeedReport:
    files: list[FileReport] = field(default_factory=list)
    textbooks_loaded: list[str] = field(default_factory=list)
    textbooks_skipped: list[str] = field(default_factory=list)
    wanted_loaded: int = 0

    @property
    def total_placements(self) -> int:
        return sum(f.placements_total for f in self.files)

    def summary_lines(self) -> list[str]:
        lines = [
            f"Files: {len(self.files)}",
            f"Entries created: {sum(f.entries_created for f in self.files)}",
            f"Placements: {self.total_placements} "
            f"({sum(f.placements_matched for f in self.files)} matched, "
            f"{sum(f.placements_unassigned for f in self.files)} unassigned)",
            f"Photos: {sum(f.photos_total for f in self.files)} "
            f"({sum(f.photos_stored for f in self.files)} stored, "
            f"{sum(f.photos_recovered for f in self.files)} recovered from pptx, "
            f"{sum(f.photos_dead for f in self.files)} dead)",
            f"Textbooks loaded: {', '.join(self.textbooks_loaded) or 'none'}",
            f"Textbooks skipped (missing CSV): {', '.join(self.textbooks_skipped) or 'none'}",
            f"Wanted scientists: {self.wanted_loaded}",
        ]
        return lines


def _strip_footnotes(paragraphs: list[str]) -> tuple[list[str], bool]:
    cleaned = [_FOOTNOTE_MARKER.sub("", p) for p in paragraphs]
    changed = cleaned != paragraphs
    return cleaned, changed


def _split_multi_student(description: list[str]) -> list[list[str]]:
    """Split a description on the second-student marker into separate
    writeups.  Returns one list per student (usually just one)."""
    joined = "\n\n".join(description)
    if MULTI_STUDENT_MARKER not in joined:
        return [description]
    parts = joined.split(MULTI_STUDENT_MARKER)
    return [[p.strip() for p in part.split("\n\n") if p.strip()] for part in parts]


def _license_notice(filename: str) -> str | None:
    if filename in WIKIPEDIA_VERBATIM:
        return "Text adapted from Wikipedia, CC BY-SA (verify version at source)"
    if filename in QUOTED_SOURCE:
        return "Contains quoted source text — see Sources"
    return None


def load_textbooks(conn, manifest_path: Path, now: str, report: SeedReport) -> dict[str, int]:
    """Load textbooks + toc_rows + aliases from the manifest.  Returns
    {slug: textbook_id}.  Textbooks whose CSV is missing are skipped
    (logged), not fatal — the 3 web-sourced TOCs may not be present yet."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = manifest_path.parent
    slug_to_id: dict[str, int] = {}
    for tb in manifest["textbooks"]:
        csv_path = base / tb["csv"]
        if not csv_path.exists():
            report.textbooks_skipped.append(tb["slug"])
            continue
        try:
            rows = load_toc_file(csv_path)
        except Exception as exc:  # invalid CSV: skip loudly, don't abort seed
            report.textbooks_skipped.append(f"{tb['slug']} (invalid: {exc})")
            continue
        cur = conn.execute(
            "INSERT INTO textbooks(slug,title,author,edition,discipline,is_builtin,created_at)"
            " VALUES(?,?,?,?,?,1,?)",
            (tb["slug"], tb["title"], tb.get("author"), tb.get("edition"),
             tb.get("discipline", "physics"), now),
        )
        textbook_id = cur.lastrowid
        slug_to_id[tb["slug"]] = textbook_id
        for order, row in enumerate(rows):
            conn.execute(
                "INSERT INTO toc_rows(textbook_id,sort_order,chapter,section,topics)"
                " VALUES(?,?,?,?,?)",
                (textbook_id, order, row.chapter, row.section, row.topics),
            )
        for alias in tb.get("aliases", []):
            conn.execute(
                "INSERT INTO textbook_aliases(textbook_id,alias,ambiguous) VALUES(?,?,?)",
                (textbook_id, alias["alias"], alias["ambiguous"]),
            )
        report.textbooks_loaded.append(tb["slug"])
    conn.commit()

    for wanted in manifest.get("wanted_scientists", []):
        conn.execute(
            "INSERT INTO wanted_scientists(name,note,source) VALUES(?,?,?)",
            (wanted["name"], wanted.get("note"), wanted.get("source")),
        )
        report.wanted_loaded += 1
    conn.commit()
    return slug_to_id


def _match_toc_row(conn, textbook_id: int, chapter: int | None) -> int | None:
    if chapter is None:
        return None
    row = conn.execute(
        "SELECT id FROM toc_rows WHERE textbook_id=? AND chapter=? ORDER BY sort_order LIMIT 1",
        (textbook_id, chapter),
    ).fetchone()
    return row["id"] if row else None


# textbook_key (placements.py) -> manifest slug.  They already align.
_KEY_TO_SLUG = {
    "knight-calc-3rd": "knight-calc-3rd",
    "knight-college-2nd": "knight-college-2nd",
    "mandi-4th": "mandi-4th",
}


def _insert_entry(conn, entry: Entry, filename: str, description: list[str],
                  now: str, extra_flags: list[str], report: FileReport,
                  slug_to_id: dict[str, int], fetch_photos: bool,
                  photo_dir: Path, decks_dir: Path | None,
                  with_placements_photos: bool = True) -> None:
    corrected_name = NAME_CORRECTIONS.get(entry.name, entry.name)
    flags = list(entry.flags) + list(extra_flags)
    if corrected_name != entry.name:
        flags.append(f"name-corrected-from:{entry.name}")
        report.corrections.append(f"{entry.name} -> {corrected_name}")

    cur = conn.execute(
        "INSERT INTO entries("
        " collection_id,scientist_name,scientist_slug,description,sources_text,"
        " contributor_name,attribution_mode,status,share_communal,communal_status,"
        " license_grant,license_notice,review_flags,seed_origin,created_at,updated_at)"
        " VALUES(NULL,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            corrected_name, slugify(corrected_name),
            "\n\n".join(description),
            "\n\n".join(entry.sources),
            "; ".join(entry.contributors) or None,
            "full",  # seed policy: names kept as written
            "pending", 1, "pending",
            0, _license_notice(filename),
            json.dumps(flags), filename, now, now,
        ),
    )
    entry_id = cur.lastrowid
    report.entries_created += 1

    # Placements and photos belong to the SCIENTIST, not each writeup;
    # when a file is split into multiple student writeups, only the
    # primary one carries them (else they double-count).
    if not with_placements_photos:
        return

    # placements
    placements = parse_placements(entry.placements_raw)
    for placement in placements:
        report.placements_total += 1
        textbook_id = None
        toc_row_id = None
        if placement.textbook_key:
            slug = _KEY_TO_SLUG.get(placement.textbook_key)
            textbook_id = slug_to_id.get(slug) if slug else None
        if textbook_id:
            toc_row_id = _match_toc_row(conn, textbook_id, placement.chapter)
            report.placements_matched += 1
        else:
            report.placements_unassigned += 1
        section_label = placement.chapter_label or placement.extra_label
        if placement.sections:
            section_label = (
                (section_label + " " if section_label else "")
                + "s" + ",".join(str(s) for s in placement.sections)
            )
        conn.execute(
            "INSERT INTO placements("
            " entry_id,textbook_id,toc_row_id,chapter,section_label,raw_line,flags)"
            " VALUES(?,?,?,?,?,?,?)",
            (entry_id, textbook_id, toc_row_id, placement.chapter,
             section_label, placement.raw_line, json.dumps(placement.flags)),
        )

    # photos
    for index, url in enumerate(entry.photos):
        report.photos_total += 1
        stored = None
        status = "pending"
        if fetch_photos:
            try:
                stored = photos_mod.fetch_photo(url, photo_dir)
                status = "stored"
                report.photos_stored += 1
            except Exception:
                status = "dead"
                report.photos_dead += 1
        conn.execute(
            "INSERT INTO photos("
            " entry_id,original_url,file_path,sha256,content_type,width,height,"
            " is_primary,fetch_status,attribution,license,license_verified)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                entry_id, url,
                str(stored.path.relative_to(photo_dir)) if stored else None,
                stored.sha256 if stored else None,
                stored.content_type if stored else None,
                stored.width if stored else None,
                stored.height if stored else None,
                1 if index == 0 else 0,
                status,
                url,  # attribution = origin link (unverified)
                "source unverified - origin link only",
            ),
        )

    # recover dead photos from the original pptx deck, if available
    if fetch_photos and decks_dir and report.photos_dead and not report.photos_stored:
        _recover_from_deck(conn, entry_id, corrected_name, decks_dir, photo_dir, report)


def _recover_from_deck(conn, entry_id: int, name: str, decks_dir: Path,
                       photo_dir: Path, report: FileReport) -> None:
    deck = decks_dir / f"{name}.pptx"
    if not deck.exists():
        return
    try:
        images = photos_mod.extract_pptx_images(deck)
    except Exception:
        return
    for data, _ in images[:1]:  # the primary image is enough for a slide
        try:
            stored = photos_mod.store_bytes(data, photo_dir)
        except Exception:
            continue
        conn.execute(
            "INSERT INTO photos("
            " entry_id,original_url,file_path,sha256,content_type,width,height,"
            " is_primary,fetch_status,attribution,license,license_verified)"
            " VALUES(?,?,?,?,?,?,?,0,?,?,?,0)",
            (entry_id, None, str(stored.path.relative_to(photo_dir)), stored.sha256,
             stored.content_type, stored.width, stored.height,
             "recovered", "recovered from original slide deck",
             "source unverified - recovered from pptx"),
        )
        report.photos_recovered += 1


def seed_import(conn, *, scientists_dir: Path, manifest_path: Path,
                photo_dir: Path, now: str, decks_dir: Path | None = None,
                fetch_photos: bool = True) -> SeedReport:
    """Run the full seed.  Idempotent: clears prior seed rows first."""
    report = SeedReport()
    # Idempotency: remove prior seed entries (cascades to placements/photos)
    # and the builtin textbooks/wanted list.
    conn.execute("DELETE FROM entries WHERE seed_origin IS NOT NULL")
    conn.execute("DELETE FROM textbooks WHERE is_builtin=1")
    conn.execute("DELETE FROM wanted_scientists")
    conn.commit()

    slug_to_id = load_textbooks(conn, manifest_path, now, report)
    photo_dir.mkdir(parents=True, exist_ok=True)

    for path in sorted(scientists_dir.glob("*.txt")):
        entry = parse_file(path)
        fields = [
            ("placements", bool(entry.placements_raw)),
            ("description", bool(entry.description)),
            ("sources", bool(entry.sources)),
            ("photo", bool(entry.photos)),
            ("contributors", bool(entry.contributors)),
        ]
        file_report = FileReport(
            filename=path.name,
            name=NAME_CORRECTIONS.get(entry.name, entry.name),
            fields_present=[n for n, present in fields if present],
            fields_missing=[n for n, present in fields if not present],
        )
        if path.name in REVIEW_NOTES:
            file_report.flags.append(REVIEW_NOTES[path.name])

        description, footnotes_stripped = _strip_footnotes(entry.description)
        student_writeups = _split_multi_student(description)
        extra_flags: list[str] = []
        if footnotes_stripped:
            extra_flags.append("footnote-markers-stripped")
        if len(student_writeups) > 1:
            extra_flags.append("split-multi-student")
        if path.name in WIKIPEDIA_VERBATIM or path.name in QUOTED_SOURCE:
            extra_flags.append("verbatim-from-source")

        for index, writeup in enumerate(student_writeups):
            _insert_entry(
                conn, entry, path.name, writeup, now,
                extra_flags + file_report.flags, file_report,
                slug_to_id, fetch_photos, photo_dir, decks_dir,
                with_placements_photos=(index == 0),
            )
        report.files.append(file_report)
        conn.commit()

    return report
