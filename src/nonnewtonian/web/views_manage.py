"""Create a class collection and manage it via a magic-link token.

No accounts: the manage token in the URL is the capability.  The token
is shown once on the welcome page (with a copy button and a pre-filled
mailto so the teacher can self-email it) and stored only as a hash.
"""

from __future__ import annotations

from urllib.parse import quote

from flask import (
    Blueprint, abort, current_app, redirect, render_template, request, url_for,
)

from .. import collections_repo as repo
from ..toc import TocError, load_toc
from . import queries as q
from . import limiter

bp = Blueprint("manage", __name__)


def _db():
    return current_app.get_db()


def _load_collection(token):
    row = repo.collection_by_manage_token(_db(), token)
    if not row:
        abort(404)
    return row


@bp.route("/new")
def new():
    return render_template("new.html", textbooks=q.list_textbooks(_db()))


@bp.route("/new", methods=["POST"])
@limiter.limit("10 per hour")
def create():
    conn = _db()
    name = request.form.get("name", "").strip()
    if not name:
        return render_template("new.html", textbooks=q.list_textbooks(conn),
                               error="Please give your class a name."), 400

    textbook_id, error = _resolve_textbook(conn)
    if error:
        return render_template("new.html", textbooks=q.list_textbooks(conn),
                               error=error), 400

    try:
        created = repo.create_collection(
            conn, name=name,
            teacher_name=request.form.get("teacher_name"),
            teacher_email=request.form.get("teacher_email"),
            textbook_id=textbook_id, now=current_app.utcnow(),
            share_default=bool(request.form.get("share_default")),
        )
    except repo.CollectionError as exc:
        return render_template("new.html", textbooks=q.list_textbooks(conn),
                               error=str(exc)), 400
    return redirect(url_for("manage.welcome", token=created.manage_token))


def _resolve_textbook(conn) -> tuple[int | None, str | None]:
    """From the /new form: pick a builtin, paste a TOC CSV, or paste a
    plain chapter list (the minimal TOC builder)."""
    mode = request.form.get("textbook_mode", "builtin")
    if mode == "builtin":
        slug = request.form.get("textbook_slug")
        tb = q.textbook_by_slug(conn, slug) if slug else None
        if not tb:
            return None, "Please choose a textbook."
        return tb["id"], None

    title = request.form.get("custom_title", "").strip()
    if not title:
        return None, "Please name your textbook."

    if mode == "csv":
        try:
            rows = load_toc(request.form.get("toc_csv", ""))
        except TocError as exc:
            return None, f"That table of contents didn't load: {exc}"
    elif mode == "chapters":
        rows = _rows_from_chapter_lines(request.form.get("chapter_lines", ""))
        if not rows:
            return None, "Please paste at least one chapter title, one per line."
    else:
        return None, "Unknown textbook option."

    from ..slugs import slugify
    import secrets as _s
    slug = f"{slugify(title)[:32] or 'textbook'}-{_s.token_hex(3)}"
    now = current_app.utcnow()
    cur = conn.execute(
        "INSERT INTO textbooks(slug,title,author,is_builtin,created_at) "
        "VALUES(?,?,?,0,?)",
        (slug, title, request.form.get("custom_author", "").strip() or None, now),
    )
    tb_id = cur.lastrowid
    for order, row in enumerate(rows):
        conn.execute(
            "INSERT INTO toc_rows(textbook_id,sort_order,chapter,section,topics) "
            "VALUES(?,?,?,?,?)",
            (tb_id, order, row.chapter, row.section, row.topics),
        )
    conn.commit()
    return tb_id, None


def _rows_from_chapter_lines(text: str):
    """One chapter title per line -> TocRow list (numbers inferred)."""
    from ..toc import TocRow
    rows = []
    for i, line in enumerate((text or "").splitlines(), start=1):
        title = line.strip()
        if title:
            rows.append(TocRow(chapter=len(rows) + 1, section=None, topics=title))
    return rows


@bp.route("/manage/<token>/welcome")
def welcome(token):
    coll = _load_collection(token)
    student_url = _abs(url_for("cls.submit", slug=coll["slug"]))
    manage_url = _abs(url_for("manage.dashboard", token=token))
    mailto = _self_mailto(coll, student_url, manage_url)
    return render_template("welcome.html", coll=coll, token=token,
                           student_url=student_url, manage_url=manage_url, mailto=mailto)


@bp.route("/manage/<token>")
def dashboard(token):
    coll = _load_collection(token)
    conn = _db()
    pending = [q._row_to_entry(conn, r) for r in conn.execute(
        "SELECT * FROM entries WHERE collection_id=? AND status='pending' "
        "ORDER BY created_at", (coll["id"],))]
    approved = conn.execute(
        "SELECT count(*) FROM entries WHERE collection_id=? AND status='approved'",
        (coll["id"],)).fetchone()[0]
    student_url = _abs(url_for("cls.submit", slug=coll["slug"]))
    embed_snippet = _embed_snippet(coll["slug"])
    return render_template("manage.html", coll=coll, token=token, pending=pending,
                           approved=approved, student_url=student_url,
                           embed_snippet=embed_snippet)


@bp.route("/manage/<token>/entry/<int:entry_id>/<action>", methods=["POST"])
def moderate(token, entry_id, action):
    coll = _load_collection(token)
    if action not in ("approve", "reject"):
        abort(400)
    conn = _db()
    row = conn.execute(
        "SELECT id FROM entries WHERE id=? AND collection_id=?",
        (entry_id, coll["id"])).fetchone()
    if not row:
        abort(404)
    now = current_app.utcnow()
    # Only offer to the communal site if the class opted in AND the student
    # granted the license; a crafted POST can't bypass either.
    entry_full = conn.execute("SELECT license_grant FROM entries WHERE id=?", (entry_id,)).fetchone()
    share = (bool(request.form.get("share_communal")) and bool(coll["share_default"])
             and bool(entry_full["license_grant"]))
    if action == "approve":
        conn.execute(
            "UPDATE entries SET status='approved', approved_at=?, updated_at=?, "
            "share_communal=?, communal_status=? WHERE id=?",
            (now, now, 1 if share else 0, "pending" if share else "none", entry_id))
    else:
        conn.execute(
            "UPDATE entries SET status='rejected', updated_at=? WHERE id=?", (now, entry_id))
    conn.commit()
    return redirect(url_for("manage.dashboard", token=token))


@bp.route("/manage/<token>/settings", methods=["POST"])
def settings(token):
    coll = _load_collection(token)
    conn = _db()
    max_attr = request.form.get("max_attribution", coll["max_attribution"])
    if max_attr not in repo.MAX_ATTRIBUTION_ORDER:
        max_attr = coll["max_attribution"]
    conn.execute(
        "UPDATE collections SET submissions_open=?, allow_photo_upload=?, "
        "share_default=?, max_attribution=? WHERE id=?",
        (1 if request.form.get("submissions_open") else 0,
         1 if request.form.get("allow_photo_upload") else 0,
         1 if request.form.get("share_default") else 0,
         max_attr, coll["id"]))
    conn.commit()
    return redirect(url_for("manage.dashboard", token=token))


@bp.route("/manage/<token>/delete", methods=["POST"])
def delete(token):
    coll = _load_collection(token)
    if request.form.get("confirm") != coll["slug"]:
        abort(400)
    conn = _db()
    # Remove photo files for this class's private (non-communal) entries —
    # but photos are content-addressed and SHARED across entries/classes,
    # so only unlink a file once NO surviving row references it (M4 review:
    # unconditional unlink corrupted another class's page). Collect
    # candidate paths, delete the rows, then unlink the now-unreferenced.
    from pathlib import Path
    photo_dir = Path(current_app.config["PHOTO_DIR"]).resolve()
    candidates = {row["file_path"] for row in conn.execute(
        "SELECT ph.file_path FROM photos ph JOIN entries e ON ph.entry_id=e.id "
        "WHERE e.collection_id=? AND ph.file_path IS NOT NULL AND e.communal_status='none'",
        (coll["id"],))}
    conn.execute("DELETE FROM collections WHERE id=?", (coll["id"],))  # cascades photos rows
    for path in candidates:
        still = conn.execute(
            "SELECT 1 FROM photos WHERE file_path=? LIMIT 1", (path,)).fetchone()
        if not still:
            _safe_unlink(photo_dir, path)
    conn.commit()
    return render_template("deleted.html", name=coll["name"])


@bp.route("/manage/<token>/kit.txt")
def kit(token):
    coll = _load_collection(token)
    student_url = _abs(url_for("cls.submit", slug=coll["slug"]))
    manage_url = _abs(url_for("manage.dashboard", token=token))
    body = render_template("kit.txt", coll=coll, student_url=student_url, manage_url=manage_url)
    return current_app.response_class(body, mimetype="text/plain",
        headers={"Content-Disposition": "attachment; filename=class-kit.txt"})


# --- helpers --------------------------------------------------------------

def _abs(path: str) -> str:
    base = current_app.config.get("SITE_URL", "").rstrip("/")
    return base + path if base else path


def _self_mailto(coll, student_url, manage_url) -> str:
    subject = quote(f"Your {current_app.config['SITE_NAME']} links for {coll['name']}")
    body = quote(
        f"Student submission link (share with your class):\n{student_url}\n\n"
        f"Your private management link (keep this one to yourself):\n{manage_url}\n")
    to = coll["teacher_email"] or ""
    return f"mailto:{to}?subject={subject}&body={body}"


def _embed_snippet(slug: str) -> str:
    url = _abs(url_for("cls.embed", slug=slug))
    return f'<iframe src="{url}" style="width:100%;border:0;" title="Class collection"></iframe>'


def _safe_unlink(base, relpath):
    from pathlib import Path
    target = (Path(base) / relpath).resolve()
    if base in target.parents and target.is_file():
        target.unlink()
