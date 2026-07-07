"""nonnewtonian: the NonNewtonian Physicists core library.

Parsing, textbook placement, TOC validation, photo handling, and PPTX
slide generation — the tested port of IntroductoryPhysics/makesyllabus.py
that the hosted app (and its seed importer) builds on.
"""

from .parser import Entry, ParseError, entry_to_text, parse, parse_file
from .placements import KNOWN_TEXTBOOKS, Placement, parse_placement, parse_placements
from .slides import DeckChapter, build_deck, build_entry_slide, save_presentation
from .toc import TocError, TocRow, load_toc, load_toc_file, repair_doubled_quotes

__all__ = [
    "Entry",
    "ParseError",
    "parse",
    "parse_file",
    "entry_to_text",
    "Placement",
    "parse_placement",
    "parse_placements",
    "KNOWN_TEXTBOOKS",
    "TocRow",
    "TocError",
    "load_toc",
    "load_toc_file",
    "repair_doubled_quotes",
    "DeckChapter",
    "build_entry_slide",
    "build_deck",
    "save_presentation",
]
