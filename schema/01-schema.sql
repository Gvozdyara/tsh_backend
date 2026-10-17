-- ---------------------------------------------------------------------------
-- TSH knowledge base schema.
--
-- Consumed twice:
--   1. by the postgres image entrypoint on a fresh data volume
--      (/docker-entrypoint-initdb.d/01-schema.sql, mounted from tsh_manifest/db)
--   2. by the backend on every start (app/dbmigrate.py) so an existing volume
--      is always brought up to date. Every statement is idempotent.
--
-- Search design (this replaces the legacy natasha/pymorphy2 index):
--   search_text : every translation of the article + its parent tree path,
--                 refreshed by the backend whenever something changes
--   tsv_<lang>  : to_tsvector(<lang config>, search_text), maintained by a
--                 trigger -> real stemming/NLP search per UI language
--   gin_trgm    : fast substring ("exact") search and fuzzy ranking
-- ---------------------------------------------------------------------------

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE OR REPLACE FUNCTION tsh_refresh_search() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.search_text := coalesce(NEW.search_text, '');
    NEW.tsv_ru := to_tsvector('russian', NEW.search_text);
    NEW.tsv_en := to_tsvector('english', NEW.search_text);
    NEW.tsv_es := to_tsvector('spanish', NEW.search_text);
    RETURN NEW;
END;
$$;

CREATE TABLE IF NOT EXISTS articles (
    id           TEXT PRIMARY KEY,
    parent_id    TEXT REFERENCES articles (id) ON DELETE RESTRICT,
    sort_index   INTEGER,
    -- materialised tree path, e.g. 'MAIN / GSM / СТАТУС GSM'
    path         TEXT NOT NULL DEFAULT '',
    -- searchable projection of all translations + the parent tree
    search_text  TEXT NOT NULL DEFAULT '',
    tsv_ru       tsvector,
    tsv_en       tsvector,
    tsv_es       tsvector,
    -- legacy 'created' timestamp of the record
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- inserted_at = first time this backend saw the row
    inserted_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- updated_at = last change of the row (or of its position in the tree)
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at   TIMESTAMPTZ,
    CONSTRAINT articles_no_self_parent CHECK (id <> parent_id)
);

DROP TRIGGER IF EXISTS trg_articles_search ON articles;
CREATE TRIGGER trg_articles_search
    BEFORE INSERT OR UPDATE OF search_text ON articles
    FOR EACH ROW EXECUTE FUNCTION tsh_refresh_search();

CREATE INDEX IF NOT EXISTS ix_articles_parent ON articles (parent_id);
CREATE INDEX IF NOT EXISTS ix_articles_parent_sort
    ON articles (parent_id, sort_index, updated_at DESC);
CREATE INDEX IF NOT EXISTS ix_articles_trgm
    ON articles USING gin (search_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS ix_articles_tsv_ru ON articles USING gin (tsv_ru);
CREATE INDEX IF NOT EXISTS ix_articles_tsv_en ON articles USING gin (tsv_en);
CREATE INDEX IF NOT EXISTS ix_articles_tsv_es ON articles USING gin (tsv_es);

CREATE TABLE IF NOT EXISTS article_translations (
    article_id        TEXT NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
    lang              TEXT NOT NULL,
    name              TEXT NOT NULL DEFAULT '',
    content           TEXT NOT NULL DEFAULT '',
    name_edited_at    TIMESTAMPTZ,
    content_edited_at TIMESTAMPTZ,
    name_author       TEXT,
    content_author    TEXT,
    PRIMARY KEY (article_id, lang),
    CONSTRAINT translations_lang_check CHECK (lang IN ('ru', 'en', 'es'))
);

CREATE INDEX IF NOT EXISTS ix_translations_lang ON article_translations (lang);
CREATE INDEX IF NOT EXISTS ix_translations_name_trgm
    ON article_translations USING gin (name gin_trgm_ops);

-- Small audit trail: powers "Last added" / "Mark updates as read".
CREATE TABLE IF NOT EXISTS audit_log (
    id         BIGSERIAL PRIMARY KEY,
    article_id TEXT,
    actor      TEXT,
    action     TEXT NOT NULL,
    payload    JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_audit_created ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_audit_article ON audit_log (article_id);

-- Application users. Passwords are bcrypt hashes, never plain text. The
-- ``TSH_USERS`` environment variable is only used to bootstrap missing
-- accounts on start; after that the database is the source of truth.
--   user      - read only
--   moderator - create / edit / move / reorder articles
--   admin     - everything, including delete, import/export and user roles
--
-- is_approved: self-registered accounts start unapproved and cannot sign in or
-- obtain a session until an admin approves them. Bootstrap accounts from
-- TSH_USERS are approved. (Pre-existing installations are backfilled as
-- approved by the ALTER below.)
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',
    is_active     BOOLEAN NOT NULL DEFAULT true,
    is_approved   BOOLEAN NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_login_at TIMESTAMPTZ,
    CONSTRAINT users_username_not_blank CHECK (btrim(username) <> ''),
    CONSTRAINT users_password_hash_not_blank CHECK (password_hash <> ''),
    CONSTRAINT users_role_check CHECK (role IN ('admin', 'moderator', 'user'))
);

-- Existing volumes: add the approval flag and trust the accounts that were
-- already there. New rows default to unapproved.
ALTER TABLE users ADD COLUMN IF NOT EXISTS is_approved BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE users ALTER COLUMN is_approved SET DEFAULT false;

-- Case-insensitive unique logins: "Admin" and "admin" are the same account.
CREATE UNIQUE INDEX IF NOT EXISTS ux_users_username_lower ON users (lower(username));
CREATE INDEX IF NOT EXISTS ix_users_role ON users (role) WHERE is_active;
CREATE INDEX IF NOT EXISTS ix_users_approved ON users (is_approved) WHERE NOT is_approved;

-- Per user read cursor for the updates counter.
CREATE TABLE IF NOT EXISTS read_marks (
    username     TEXT PRIMARY KEY,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
