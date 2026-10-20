"""Static export of the public communal site (M7 WP10).

``export_site`` renders an EXPLICIT list of public URLs through the Flask
test client (no crawling) into a directory that works from file://, from
a GitHub Pages subpath, and as Caddy's outage fallback
(``try_files {path} {path}/index.html``): ``/a/b`` becomes
``a/b/index.html``.

What is left out, and why: class pages ``/c/*`` (teacher-owned, not
consented to a public repo), whole-book decks (regenerable, binary
churn), every form/manage/admin/healthz/search route (they need the
server), and pending/rejected entries and their photos (no public route
renders them; photos are copied only when public HTML references them).
Links to anything not exported point at the live site instead.

Fail closed: any non-200 page, an empty site, or a leak-guard hit raises
``ExportError`` and the previous export is left untouched.  The new tree
is built in ``<out>.next`` and swapped in with two renames (a directory
cannot be atomically replaced by a non-empty one on Linux), so the gap in
which ``<out>`` does not exist is two rename syscalls long.
"""

from __future__ import annotations

import csv
import html
import io
import os
import posixpath
import re
import shutil
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import db as db_mod

_PKG = Path(__file__).resolve().parent
_STYLE_CSS = _PKG / "web" / "static" / "style.css"
# Editable installs only (the production preflight already requires one).
_DOWN_HTML = _PKG.parents[1] / "deploy" / "caddy" / "down.html"

STATIC_PAGES = ["/", "/scientists", "/textbooks", "/wanted", "/about", "/for-teachers",
                "/for-students", "/privacy"]
# Same shape the Caddyfile serves; anything else under /photos/ is refused.
_PHOTO_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.(jpg|png|gif|webp)$")
_ATTR_RE = re.compile(r'\b(href|src|action)="([^"]*)"')
# "/recover/" with the slash: the bare /recover form holds no token and is a
# legitimate live-site link; /recover/confirm/<signed> never belongs in an export.
_FORBIDDEN_LINK_PREFIXES = ("/manage/", "/admin/", "/recover/", "/c/")
_ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)
CSV_COLUMNS = ["name", "slug", "description", "sources", "credited_as", "placements",
               "photo_file", "photo_license", "photo_attribution", "license_notice"]


class ExportError(RuntimeError):
    pass


@dataclass
class ExportReport:
    pages: int
    slides: int
    photos: int
    entries: int


def _page_file(url_path: str) -> str:
    """'/' -> 'index.html'; '/a/b' -> 'a/b/index.html'."""
    stripped = url_path.strip("/")
    return f"{stripped}/index.html" if stripped else "index.html"


def deterministic_pptx(data: bytes) -> bytes:
    """Rewrite a .pptx zip with fixed member timestamps (python-pptx stamps
    the current time on every member), same member order, DEFLATE."""
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            zinfo = zipfile.ZipInfo(info.filename, date_time=_ZIP_EPOCH)
            zinfo.compress_type = zipfile.ZIP_DEFLATED
            dst.writestr(zinfo, src.read(info))
    return out.getvalue()


def _site_path(value: str, page_dir: str, site_url: str) -> str | None:
    """The site path a link value points at, or None for an external link."""
    if value.startswith(site_url + "/"):
        return value[len(site_url):]
    if value.startswith("//") or ":" in value.split("/", 1)[0] or value.startswith("#"):
        return None  # other host, mailto:, javascript:, fragment
    if value.startswith("/"):
        return value
    return posixpath.normpath(posixpath.join("/" + page_dir, value))


def _rewrite_links(text: str, page_url: str, exported: dict[str, str], site_url: str) -> str:
    """Root-relative href/src -> relative path when exported, else the live
    site; every form action -> the live site (a static page cannot search)."""
    page_dir = posixpath.dirname(_page_file(page_url))
    live = html.escape(site_url, quote=True)

    def sub(m: re.Match) -> str:
        attr, value = m.group(1), m.group(2)
        if not value.startswith("/") or value.startswith("//"):
            return m.group(0)
        path, frag = (value.split("#", 1) + [""])[:2]
        target = exported.get(path) if attr != "action" else None
        if target is None:
            return f'{attr}="{live}{value}"'
        rel = posixpath.relpath(target, page_dir or ".")
        return f'{attr}="{rel}{"#" + frag if frag else ""}"'

    return _ATTR_RE.sub(sub, text)


def _placements_text(entry) -> str:
    out = []
    for pl in entry.placements:
        bit = pl.textbook_title or pl.raw_line
        if pl.chapter:
            bit += f" ch. {pl.chapter}"
        if pl.section_label:
            bit += f" ({pl.section_label})"
        out.append(bit)
    return "; ".join(out)


def _entries_csv(entries) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for e in entries:
        p = e.primary_photo
        w.writerow([
            e.name, e.slug, "\n\n".join(e.description), "\n\n".join(e.sources),
            e.displayed_credit() or "",  # NEVER contributor_name
            _placements_text(e),
            f"photos/{p.file_path}" if p else "", (p.license or "") if p else "",
            (p.attribution or "") if p else "", e.license_notice or "",
        ])
    return buf.getvalue().encode("utf-8")


def _forbidden_strings(conn, config, public_entries) -> list[tuple[str, str]]:
    """(needle, label) pairs that must not appear in any exported text.
    Labels never include the secret itself."""
    needles = [("csrf_token", "a CSRF token field")]
    for key in ("ADMIN_TOKEN", "SECRET_KEY"):
        if config.get(key):
            needles.append((str(config[key]), f"the {key}"))
    for (slug,) in conn.execute("SELECT slug FROM collections"):
        needles.append((slug, "a class slug"))
    for (email,) in conn.execute(
            "SELECT teacher_email FROM collections WHERE teacher_email IS NOT NULL"):
        needles.append((email, "a teacher email"))
    # A credit shown publicly by design (a full-mode name, or a one-word
    # first_initial name, which displays as itself) is not a leak.
    shown = {e.displayed_credit() for e in public_entries} - {None}
    for eid, name in conn.execute(
            "SELECT id, contributor_name FROM entries WHERE attribution_mode != 'full' "
            "AND contributor_name IS NOT NULL AND length(contributor_name) >= 4"):
        if name not in shown:
            needles.append((name, f"the contributor name of entry {eid} (not full attribution)"))
    return [(n.lower(), label) for n, label in needles]


def _texts(path: Path, rel: str):
    """Yield (where, lowercased text) for a text file, or each XML member of a .pptx."""
    if rel.endswith(".pptx"):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                if name.endswith((".xml", ".rels")):
                    yield f"{rel}:{name}", z.read(name).decode("utf-8", "replace").lower()
    elif rel.endswith((".html", ".csv", ".css")):
        yield rel, path.read_text("utf-8").lower()


def _leak_guard(tmp: Path, expected: set[str], slugs: set[str], needles, site_url: str) -> None:
    actual = {p.relative_to(tmp).as_posix() for p in tmp.rglob("*") if not p.is_dir()}
    if actual != expected:
        raise ExportError(f"leak guard: unexpected files {sorted(actual - expected)[:5]}, "
                          f"missing {sorted(expected - actual)[:5]}")
    sci_dirs = {p.name for p in (tmp / "scientists").iterdir() if p.is_dir()}
    if sci_dirs != slugs:
        raise ExportError("leak guard: scientist directories differ from the approved slug set")
    for rel in sorted(actual):
        for where, text in _texts(tmp / rel, rel):
            for needle, label in needles:
                # Jinja autoescapes ' and & in HTML, so check the escaped form too.
                if needle in text or html.escape(needle, quote=True) in text:
                    raise ExportError(f"leak guard: {where} contains {label}")
            if rel.endswith(".html"):
                page_dir = posixpath.dirname(rel)
                for _, value in _ATTR_RE.findall(text):
                    path = _site_path(value, page_dir, site_url.lower())
                    if path and path.startswith(_FORBIDDEN_LINK_PREFIXES):
                        raise ExportError(f"leak guard: {where} links to {path.split('/')[1]}")


def export_site(db_path, photo_dir, out_dir, site_url: str) -> ExportReport:
    from .web import create_app, queries as q

    site_url = site_url.rstrip("/")
    if not site_url.startswith(("https://", "http://")):
        raise ExportError("--site-url must be an absolute http(s) URL")
    db_path, photo_dir, out_dir = Path(db_path), Path(photo_dir).resolve(), Path(out_dir)
    if not db_path.exists():  # connect() would silently create an empty file
        raise ExportError(f"database missing at {db_path}")
    if not _DOWN_HTML.exists():
        raise ExportError(f"{_DOWN_HTML} missing; the install must be editable")

    app = create_app({"DB_PATH": str(db_path), "PHOTO_DIR": str(photo_dir),
                      "SITE_URL": site_url, "STATIC_EXPORT": True, "TESTING": True,
                      "WTF_CSRF_ENABLED": False})
    conn = db_mod.connect(db_path)
    try:
        scientists = q.all_approved_scientists(conn)
        if not scientists:
            raise ExportError("no approved entries; refusing to export an empty site")
        slugs = sorted(e.slug for e in scientists)
        entries = [w for s in slugs for w in q.approved_entries_by_slug(conn, s)]
        tb_slugs = [t["slug"] for t in q.public_textbooks(conn)]
        needles = _forbidden_strings(conn, app.config, entries)
    finally:
        conn.close()

    pages = (STATIC_PAGES + [f"/scientists/{s}" for s in slugs]
             + [f"/textbooks/{t}" for t in tb_slugs])
    slides = [f"/scientists/{s}/slide.pptx" for s in slugs]
    client = app.test_client()
    rendered: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for url in pages + slides:
        resp = client.get(url)
        if resp.status_code != 200:
            raise ExportError(f"GET {url} returned {resp.status_code}")
        if url.endswith(".pptx"):
            files[url.lstrip("/")] = deterministic_pptx(resp.data)
        else:
            rendered[url] = resp.get_data(as_text=True)

    exported = {u: _page_file(u) for u in pages}
    exported.update({u: u.lstrip("/") for u in slides})
    exported["/static/style.css"] = "static/style.css"
    photos: set[str] = set()
    for text in rendered.values():
        for _, value in _ATTR_RE.findall(text):
            if value.startswith("/photos/"):
                rel = value[len("/photos/"):]
                if not _PHOTO_RE.match(rel):
                    raise ExportError(f"unexpected photo path {value!r}")
                src = (photo_dir / rel).resolve()
                if photo_dir not in src.parents or not src.is_file():
                    raise ExportError(f"referenced photo missing or outside the tree: {rel}")
                photos.add(rel)
                exported[value] = f"photos/{rel}"
    for e in entries:
        if e.primary_photo and e.primary_photo.file_path not in photos:
            raise ExportError(f"CSV photo for {e.slug} is not on any public page")

    for url, text in rendered.items():
        files[_page_file(url)] = _rewrite_links(text, url, exported, site_url).encode("utf-8")
    files["static/style.css"] = _STYLE_CSS.read_bytes()
    files["down.html"] = _DOWN_HTML.read_bytes()
    files["entries.csv"] = _entries_csv(entries)
    files[".nojekyll"] = b""
    for rel in photos:
        files[f"photos/{rel}"] = (photo_dir / rel).read_bytes()

    tmp = Path(str(out_dir) + ".next")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for rel, data in sorted(files.items()):
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        _leak_guard(tmp, set(files), set(slugs), needles, site_url)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    prev = Path(str(out_dir) + ".prev")
    shutil.rmtree(prev, ignore_errors=True)
    if out_dir.exists():
        os.replace(out_dir, prev)
    os.replace(tmp, out_dir)
    shutil.rmtree(prev, ignore_errors=True)
    return ExportReport(pages=len(pages), slides=len(slides), photos=len(photos),
                        entries=len(entries))
