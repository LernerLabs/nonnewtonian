"""ASCII slugs from Unicode display names.

Used for URLs and on-disk filenames, so a name like "Nguyễn Văn Hiệu"
or "Shin'ichirō Tomonaga" never reaches a path or URL verbatim (the
original pipeline's path-traversal and fragile-URL bugs).  No external
dependency: NFKD decomposition strips most diacritics, and a small map
covers letters NFKD leaves intact (ø, ł, ...).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

# Letters that NFKD does not decompose to ASCII + combining marks.
_SPECIAL = {
    "ø": "o", "Ø": "o", "ł": "l", "Ł": "l", "đ": "d", "Đ": "d",
    "ð": "d", "Ð": "d", "þ": "th", "Þ": "th", "ß": "ss",
    "æ": "ae", "Æ": "ae", "œ": "oe", "Œ": "oe", "ı": "i", "ħ": "h",
}
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Return a lowercase ASCII ``a-z0-9-`` slug for ``text``.

    When a name has no ASCII letters (e.g. a name written entirely in
    Chinese or Arabic script), NFKD leaves nothing, so we fall back to a
    short stable hash of the ORIGINAL name — ``"s-<hash>"`` — rather than
    a shared constant.  A shared constant collapsed every such name to one
    slug, which made distinct scientists collide: false "already
    submitted" rejections and per-chapter dedup dropping all but one
    (M4 review).  The hash keeps distinct names distinct and stable.
    """
    mapped = "".join(_SPECIAL.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", mapped)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_SLUG.sub("-", ascii_text).strip("-")
    if slug:
        return slug
    normalized = unicodedata.normalize("NFC", text).strip()
    if normalized:
        return "s-" + hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:10]
    return "x"
