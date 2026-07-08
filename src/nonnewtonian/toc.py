"""Load and validate textbook table-of-contents CSVs.

The canonical format is the one IntroductoryPhysics used since 2016::

    Chapter,Section,Topics
    1,,"Motion, Velocity, Acceleration"
    2,,"1D Kinematics"

Validation exists because of a real, silent failure: the original
MandI4thEdition.csv has a stray doubled quote terminating every Topics
field, which makes csv fold 23 chapter lines into 13 mangled records —
every even-numbered chapter disappears into the previous row's Topics,
with no error raised.  The old pipeline would have published that.
``load_toc`` rejects exactly this class loudly; ``repair_doubled_quotes``
fixes the known seed file.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass


class TocError(ValueError):
    """A TOC CSV failed validation.  Message is written for teachers."""


@dataclass
class TocRow:
    chapter: int
    section: int | None
    topics: str


EXPECTED_HEADER = ["Chapter", "Section", "Topics"]


def load_toc(text: str) -> list[TocRow]:
    """Parse and validate TOC CSV text; raise TocError with a readable
    message on anything that would render wrong later."""
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise TocError("The file is empty.") from None
    if [h.strip() for h in header] != EXPECTED_HEADER:
        raise TocError(
            "The first line must be exactly 'Chapter,Section,Topics', but "
            f"got {','.join(header)!r}."
        )

    rows: list[TocRow] = []
    previous_chapter = 0
    for line_number, record in enumerate(reader, start=2):
        if not record or all(not cell.strip() for cell in record):
            continue
        if len(record) != 3:
            raise TocError(
                f"Line {line_number}: expected 3 columns "
                f"(Chapter, Section, Topics), got {len(record)}."
            )
        raw_chapter, raw_section, topics = (cell.strip() for cell in record)
        if any("\n" in cell for cell in record):
            raise TocError(
                f"Line {line_number}: a field contains a line break. This "
                "usually means a quoting problem in the CSV (a previous "
                "chapter may have swallowed this one)."
            )
        try:
            chapter = int(raw_chapter)
        except ValueError:
            raise TocError(
                f"Line {line_number}: Chapter must be a whole number, "
                f"got {raw_chapter!r}."
            ) from None
        if chapter < previous_chapter:
            raise TocError(
                f"Line {line_number}: chapter {chapter} comes after chapter "
                f"{previous_chapter}; chapters must not decrease."
            )
        section: int | None = None
        if raw_section:
            try:
                section = int(raw_section)
            except ValueError:
                raise TocError(
                    f"Line {line_number}: Section must be a whole number or "
                    f"empty, got {raw_section!r}."
                ) from None
        rows.append(TocRow(chapter=chapter, section=section, topics=topics))
        previous_chapter = chapter

    if not rows:
        raise TocError("No chapter rows found under the header line.")
    return rows


def load_toc_file(path) -> list[TocRow]:
    with open(path, encoding="utf-8") as handle:
        return load_toc(handle.read())


def repair_doubled_quotes(text: str) -> str:
    """Fix the MandI4thEdition.csv corruption: every data line ends with
    a doubled closing quote (``...Motion""``).  Strips one trailing quote
    from lines that end with exactly two."""
    repaired = []
    for line in text.split("\n"):
        stripped = line.rstrip()
        if (
            stripped.endswith('""')
            and not stripped.endswith('"""')
            and not stripped.endswith(',""')  # a legitimately empty quoted field
        ):
            line = stripped[:-1]
        repaired.append(line)
    return "\n".join(repaired)
