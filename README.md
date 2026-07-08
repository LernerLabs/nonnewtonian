# NonNewtonian Physicists

Put scientists who don't look like Isaac Newton into the textbook your
class already uses.

This repository is the third act of the
[Decolonising Introductory Physics](https://github.com/mglerner/IntroductoryPhysics)
project: a hosted web app where students research diverse scientists and
submit them against the actual table of contents of their class's
textbook, teachers moderate via a magic link, and every approved entry
lands on a chapter-indexed public page with a ready-to-teach PowerPoint
slide. No accounts; nothing for teachers or students to install.

Status: **M6 of 8 done** — the core library (`nonnewtonian`, ported from
the original `makesyllabus.py` with every audited bug fixed and the 39
real scientist files as its acceptance suite) plus the full Flask app:
the browse-first read-only site, token-gated admin queue, class
collections via magic link, communal opt-in, and the auto-resizing
WordPress embed. Each milestone was shaped by an adversarial review and
all confirmed findings fixed. What remains is **M7** (deploy) and
**M8** (one-command subject-specific instances). The full implementation
plan (design, milestones, and the adversarial review that shaped it)
lives in Michael's reports repo.

## The library

- `nonnewtonian.parser` — parse and round-trip the scientist text-file
  format (header variance, repeated-block merge, paragraph unwrap,
  hyphen-split URL rejoin). `parse(entry_to_text(e)) == e` is tested
  over the whole corpus: an entry can always leave the system as a
  plain-text file.
- `nonnewtonian.placements` — textbook placement lines (all seven
  grammars in the corpus) with alias matching and review flags. Nothing
  is ever silently dropped; the six placements the original pipeline
  lost all surface.
- `nonnewtonian.toc` — TOC CSV loading with teacher-readable
  validation, including rejection of the quoting corruption that
  silently swallowed half the Matter & Interactions chapters.
- `nonnewtonian.photos` — fetch/validate/normalize/store photos
  (content-hash storage, magic-byte checks, decompression-bomb limits,
  private-address rejection) plus recovery of images embedded in the
  old .pptx decks — the only surviving copies of rotted photo URLs.
- `nonnewtonian.slides` — per-scientist slides and chapter-ordered
  decks via python-pptx: aspect-fit images, formatted notes, text
  slides for photo-less scientists, divider slides per chapter.

## Development

```
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

`tests/fixtures/` carries the 39 real scientist files, both original
TOC CSVs (including the broken M&I one, kept broken on purpose as a
regression fixture), and two original slide decks. `examples/` holds
generated sample output.

## License

GPL-3.0-or-later. Scientist descriptions in `tests/fixtures/` were
contributed by students of the original project and include text
derived from Wikipedia and other cited sources; see each file's Sources
section.
