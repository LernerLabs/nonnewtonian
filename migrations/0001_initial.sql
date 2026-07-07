-- NonNewtonian Physicists — initial schema (M2).
--
-- Design follows the implementation plan's data model. Notes:
--   * section is nullable everywhere (both shipped TOCs have 0 sections).
--   * entries.collection_id NULL means a communal-pool entry (no class).
--   * placements.textbook_id NULL preserves placements in textbooks we
--     have no TOC for — nothing is ever dropped.
--   * manage/admin tokens are stored as sha256 hashes, never plaintext.
--   * timestamps are UTC ISO-8601 text (SQLite has no native datetime).
--   * review_flags / photos.* JSON columns are TEXT holding JSON.

CREATE TABLE textbooks (
    id           INTEGER PRIMARY KEY,
    slug         TEXT NOT NULL UNIQUE,
    title        TEXT NOT NULL,
    author       TEXT,
    edition      TEXT,
    discipline   TEXT NOT NULL DEFAULT 'physics',
    is_builtin   INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE toc_rows (
    id           INTEGER PRIMARY KEY,
    textbook_id  INTEGER NOT NULL REFERENCES textbooks(id) ON DELETE CASCADE,
    sort_order   INTEGER NOT NULL,
    chapter      INTEGER NOT NULL,
    section      INTEGER,
    topics       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_toc_rows_textbook ON toc_rows(textbook_id, sort_order);

CREATE TABLE textbook_aliases (
    id           INTEGER PRIMARY KEY,
    textbook_id  INTEGER NOT NULL REFERENCES textbooks(id) ON DELETE CASCADE,
    alias        TEXT NOT NULL,
    ambiguous    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_textbook_aliases_textbook ON textbook_aliases(textbook_id);

CREATE TABLE collections (
    id                INTEGER PRIMARY KEY,
    slug              TEXT NOT NULL UNIQUE,
    name              TEXT NOT NULL,
    teacher_name      TEXT,
    teacher_email     TEXT,
    manage_token_hash TEXT NOT NULL UNIQUE,
    textbook_id       INTEGER REFERENCES textbooks(id) ON DELETE SET NULL,
    submissions_open  INTEGER NOT NULL DEFAULT 1,
    allow_photo_upload INTEGER NOT NULL DEFAULT 1,
    share_default     INTEGER NOT NULL DEFAULT 0,
    -- Cap on how much of their name a student may show. New collections
    -- default to the K-12-safe 'first_initial'.
    max_attribution   TEXT NOT NULL DEFAULT 'first_initial',
    created_at        TEXT NOT NULL
);

CREATE TABLE entries (
    id               INTEGER PRIMARY KEY,
    collection_id    INTEGER REFERENCES collections(id) ON DELETE CASCADE,
    scientist_name   TEXT NOT NULL,
    scientist_slug   TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',   -- paragraphs, blank-line separated
    sources_text     TEXT NOT NULL DEFAULT '',
    contributor_name TEXT,
    attribution_mode TEXT NOT NULL DEFAULT 'anonymous', -- full | first_initial | anonymous
    why_chapter      TEXT,
    wikipedia_url    TEXT,
    status           TEXT NOT NULL DEFAULT 'pending',    -- pending|approved|rejected
    share_communal   INTEGER NOT NULL DEFAULT 0,
    communal_status  TEXT NOT NULL DEFAULT 'none',       -- none|pending|approved|rejected
    license_grant    INTEGER NOT NULL DEFAULT 0,         -- student granted CC BY-SA
    license_notice   TEXT,                               -- e.g. 'CC BY-SA 3.0 (Wikipedia)'
    review_flags     TEXT NOT NULL DEFAULT '[]',         -- JSON array
    adopted_from_entry_id INTEGER REFERENCES entries(id) ON DELETE SET NULL,
    seed_origin      TEXT,                               -- original filename for seed entries
    created_at       TEXT NOT NULL,
    approved_at      TEXT,
    updated_at       TEXT NOT NULL
);
CREATE INDEX idx_entries_collection ON entries(collection_id, status);
CREATE INDEX idx_entries_communal ON entries(communal_status);
CREATE INDEX idx_entries_slug ON entries(scientist_slug);

CREATE TABLE placements (
    id            INTEGER PRIMARY KEY,
    entry_id      INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    textbook_id   INTEGER REFERENCES textbooks(id) ON DELETE SET NULL,
    toc_row_id    INTEGER REFERENCES toc_rows(id) ON DELETE SET NULL,
    chapter       INTEGER,
    section_label TEXT,
    raw_line      TEXT NOT NULL,   -- verbatim original, always kept
    flags         TEXT NOT NULL DEFAULT '[]'  -- JSON array
);
CREATE INDEX idx_placements_entry ON placements(entry_id);
CREATE INDEX idx_placements_textbook ON placements(textbook_id, chapter);

CREATE TABLE photos (
    id              INTEGER PRIMARY KEY,
    entry_id        INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    original_url    TEXT,
    file_path       TEXT,           -- content-hash relative path, NULL if unfetched
    sha256          TEXT,
    content_type    TEXT,
    width           INTEGER,
    height          INTEGER,
    is_primary      INTEGER NOT NULL DEFAULT 0,
    fetch_status    TEXT NOT NULL DEFAULT 'pending', -- pending|stored|dead|recovered
    attribution     TEXT,
    license         TEXT,
    license_verified INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_photos_entry ON photos(entry_id);

CREATE TABLE wanted_scientists (
    id      INTEGER PRIMARY KEY,
    name    TEXT NOT NULL,
    note    TEXT,
    source  TEXT
);
