"""ASCII slugs from Unicode display names.

Used for URLs and on-disk filenames, so a name like "Nguyễn Văn Hiệu"
or "Shin'ichirō Tomonaga" never reaches a path or URL verbatim (the
original pipeline's path-traversal and fragile-URL bugs).  No external
dependency: NFKD decomposition strips most diacritics, and a small map
covers letters NFKD leaves intact (ø, ł, ...).
"""

from __future__ import annotations

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

    Empty or all-non-ASCII input yields ``"x"`` so a slug is never blank
    (which would break URLs/filenames).
    """
    mapped = "".join(_SPECIAL.get(ch, ch) for ch in text)
    decomposed = unicodedata.normalize("NFKD", mapped)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    slug = _NON_SLUG.sub("-", ascii_text).strip("-")
    return slug or "x"
