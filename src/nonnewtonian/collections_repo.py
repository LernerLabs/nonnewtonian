"""Create and look up class collections and their student submissions.

A collection is one teacher's class: a public slug (student link), a
hashed manage token (teacher link), and a textbook.  Students submit
entries with ``collection_id`` set and ``status='pending'`` until the
teacher approves them onto the class's public page.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

from . import tokens
from .slugs import slugify

SLUG_RANDOM_BYTES = 6
MAX_ATTRIBUTION_ORDER = ["anonymous", "first_initial", "full"]


class CollectionError(ValueError):
    pass


@dataclass
class CreatedCollection:
    id: int
    slug: str
    manage_token: str  # plaintext, shown ONCE to the teacher


def _unique_slug(conn, name: str) -> str:
    base = slugify(name)[:40] or "class"
    # Always append short randomness: class names collide and the slug is
    # a semi-public capability (knowing it lets you submit), so it should
    # not be guessable from the class name alone.
    for _ in range(10):
        candidate = f"{base}-{secrets.token_hex(SLUG_RANDOM_BYTES)[:8]}"
        if not conn.execute(
            "SELECT 1 FROM collections WHERE slug=?", (candidate,)
        ).fetchone():
            return candidate
    raise CollectionError("could not generate a unique slug")  # pragma: no cover


def create_collection(conn, *, name: str, teacher_name: str | None,
                      teacher_email: str | None, textbook_id: int | None,
                      now: str, share_default: bool = False,
                      max_attribution: str = "first_initial") -> CreatedCollection:
    name = (name or "").strip()
    if not name:
        raise CollectionError("A class name is required.")
    if max_attribution not in MAX_ATTRIBUTION_ORDER:
        raise CollectionError("Invalid attribution setting.")
    slug = _unique_slug(conn, name)
    token = tokens.new_token()
    cur = conn.execute(
        "INSERT INTO collections(slug,name,teacher_name,teacher_email,"
        "manage_token_hash,textbook_id,share_default,max_attribution,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (slug, name, (teacher_name or "").strip() or None,
         (teacher_email or "").strip() or None, tokens.hash_token(token),
         textbook_id, 1 if share_default else 0, max_attribution, now),
    )
    conn.commit()
    return CreatedCollection(id=cur.lastrowid, slug=slug, manage_token=token)


def collection_by_manage_token(conn, token: str):
    if not token:
        return None
    row = conn.execute(
        "SELECT * FROM collections WHERE manage_token_hash=?",
        (tokens.hash_token(token),),
    ).fetchone()
    return row


def collection_by_slug(conn, slug: str):
    return conn.execute("SELECT * FROM collections WHERE slug=?", (slug,)).fetchone()


def allowed_attribution_modes(max_attribution: str) -> list[str]:
    """The attribution options a student may pick, capped by the class
    setting (a K-12 teacher can forbid full names)."""
    ceiling = MAX_ATTRIBUTION_ORDER.index(max_attribution)
    return MAX_ATTRIBUTION_ORDER[: ceiling + 1]


def duplicate_scientist(conn, collection_id: int, name: str) -> bool:
    """Has this scientist already been submitted to this class? (The
    original assignment's 'please choose someone else' request.)"""
    slug = slugify(name)
    row = conn.execute(
        "SELECT 1 FROM entries WHERE collection_id=? AND scientist_slug=? "
        "AND status != 'rejected' LIMIT 1",
        (collection_id, slug),
    ).fetchone()
    return row is not None


def clone_to_communal(conn, source_entry_id: int, now: str) -> int | None:
    """Copy a class entry into an INDEPENDENT communal entry (M5).

    The communal copy has collection_id NULL and is not cascade-deleted
    when the class is deleted, so an approved shared writeup survives the
    class that produced it (what deleted.html promises).  Photos are
    content-addressed, so the copy references the same files (delete is
    refcounted).  Idempotent: returns the existing clone's id if one was
    already made for this source."""
    existing = conn.execute(
        "SELECT id FROM entries WHERE adopted_from_entry_id=? AND collection_id IS NULL",
        (source_entry_id,)).fetchone()
    if existing:
        return existing["id"]
    src = conn.execute("SELECT * FROM entries WHERE id=?", (source_entry_id,)).fetchone()
    if src is None:
        return None
    cur = conn.execute(
        "INSERT INTO entries(collection_id,scientist_name,scientist_slug,description,"
        "sources_text,contributor_name,attribution_mode,why_chapter,wikipedia_url,status,"
        "share_communal,communal_status,license_grant,license_notice,review_flags,"
        "adopted_from_entry_id,created_at,approved_at,updated_at) "
        "VALUES(NULL,?,?,?,?,?,?,?,?,'approved',1,'approved',?,?,?,?,?,?,?)",
        (src["scientist_name"], src["scientist_slug"], src["description"], src["sources_text"],
         src["contributor_name"], src["attribution_mode"], src["why_chapter"], src["wikipedia_url"],
         src["license_grant"], src["license_notice"], src["review_flags"],
         source_entry_id, now, now, now))
    clone_id = cur.lastrowid
    for pl in conn.execute("SELECT * FROM placements WHERE entry_id=?", (source_entry_id,)):
        conn.execute(
            "INSERT INTO placements(entry_id,textbook_id,toc_row_id,chapter,section_label,"
            "raw_line,flags) VALUES(?,?,?,?,?,?,?)",
            (clone_id, pl["textbook_id"], pl["toc_row_id"], pl["chapter"],
             pl["section_label"], pl["raw_line"], pl["flags"]))
    for ph in conn.execute("SELECT * FROM photos WHERE entry_id=?", (source_entry_id,)):
        conn.execute(
            "INSERT INTO photos(entry_id,original_url,file_path,sha256,content_type,width,"
            "height,is_primary,fetch_status,attribution,license,license_verified) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (clone_id, ph["original_url"], ph["file_path"], ph["sha256"], ph["content_type"],
             ph["width"], ph["height"], ph["is_primary"], ph["fetch_status"],
             ph["attribution"], ph["license"], ph["license_verified"]))
    return clone_id


def create_submission(conn, *, collection_id: int, scientist_name: str,
                      description: str, sources_text: str, why_chapter: str | None,
                      contributor_name: str | None, attribution_mode: str,
                      license_grant: bool, now: str,
                      placement: dict | None) -> int:
    """Insert a pending student entry (+ optional placement).  Returns
    the new entry id.  Photos are attached separately by the caller."""
    cur = conn.execute(
        "INSERT INTO entries(collection_id,scientist_name,scientist_slug,description,"
        "sources_text,why_chapter,contributor_name,attribution_mode,status,"
        "share_communal,communal_status,license_grant,review_flags,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,'pending',0,'none',?,?,?,?)",
        (collection_id, scientist_name, slugify(scientist_name), description,
         sources_text, why_chapter, contributor_name, attribution_mode,
         1 if license_grant else 0, json.dumps([]), now, now),
    )
    entry_id = cur.lastrowid
    if placement:
        conn.execute(
            "INSERT INTO placements(entry_id,textbook_id,toc_row_id,chapter,"
            "section_label,raw_line,flags) VALUES(?,?,?,?,?,?,?)",
            (entry_id, placement.get("textbook_id"), placement.get("toc_row_id"),
             placement.get("chapter"), placement.get("section_label"),
             placement.get("raw_line", ""), json.dumps([])),
        )
    conn.commit()
    return entry_id
