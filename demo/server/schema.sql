-- 竹聘安決賽示範系統 SQLite schema (S01). Synthetic demo data only (DEMO_DATA=true).
-- Every case-scoped query must join through cases.owner_id; never filter only in the browser.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY,
    login_name    TEXT NOT NULL UNIQUE,
    role          TEXT NOT NULL CHECK (role IN ('employer', 'officer')),
    display_name  TEXT NOT NULL,
    is_demo_admin INTEGER NOT NULL DEFAULT 0 CHECK (is_demo_admin IN (0, 1)),
    created_at    TEXT NOT NULL
);

-- Login codes are stored as SHA-256 hashes only. single_use=1 codes are burned on first successful login.
CREATE TABLE IF NOT EXISTS login_codes (
    id          INTEGER PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    code_hash   TEXT NOT NULL,
    single_use  INTEGER NOT NULL DEFAULT 1 CHECK (single_use IN (0, 1)),
    expires_at  TEXT,
    used_at     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_login_codes_account ON login_codes(account_id);

-- The cookie holds a random token; only its SHA-256 hash is stored here.
CREATE TABLE IF NOT EXISTS sessions (
    id_hash     TEXT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cases (
    id            INTEGER PRIMARY KEY,
    owner_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    display_code  TEXT NOT NULL UNIQUE,
    label         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_cases_owner ON cases(owner_id);

-- No original file is kept: only the fields the user confirmed and a digest for duplicate detection.
CREATE TABLE IF NOT EXISTS documents (
    id                     INTEGER PRIMARY KEY,
    case_id                INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    kind                   TEXT NOT NULL,
    source_filename        TEXT,
    confirmed_fields_json  TEXT NOT NULL,
    confirmed_at           TEXT NOT NULL,
    source_digest          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_documents_case ON documents(case_id);

CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY,
    case_id          INTEGER NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
    document_id      INTEGER REFERENCES documents(id) ON DELETE SET NULL,
    rule_key         TEXT NOT NULL,
    worker           TEXT NOT NULL,
    worker_norm      TEXT NOT NULL,
    title            TEXT NOT NULL,
    event_date       TEXT,
    start_date       TEXT,
    due_date         TEXT NOT NULL,
    law_ref          TEXT,
    law_version      TEXT,
    note             TEXT,
    calc_trace_json  TEXT,
    status           TEXT NOT NULL DEFAULT 'todo'
                     CHECK (status IN ('todo', 'delegated', 'proof_submitted', 'done', 'needs_review')),
    assigned         TEXT,
    office           TEXT,
    official_url     TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tasks_case ON tasks(case_id);
-- same worker + same rule + same window in one case = same task (re-importing a document adds nothing).
-- IFNULL: a plain UNIQUE treats every NULL start_date as distinct and would let duplicates in.
CREATE UNIQUE INDEX IF NOT EXISTS ux_tasks_identity
    ON tasks(case_id, worker_norm, rule_key, IFNULL(start_date, ''), due_date);

CREATE TABLE IF NOT EXISTS proofs (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    submitted_at   TEXT NOT NULL,
    observed_date  TEXT,
    verdict        TEXT,
    confirmed_at   TEXT,
    detail_json    TEXT,
    stored_file    TEXT
);
CREATE INDEX IF NOT EXISTS ix_proofs_task ON proofs(task_id);

CREATE TABLE IF NOT EXISTS line_bindings (
    id            INTEGER PRIMARY KEY,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    line_user_id  TEXT NOT NULL,
    bound_at      TEXT NOT NULL,
    revoked_at    TEXT
);
-- one active binding per LINE user
CREATE UNIQUE INDEX IF NOT EXISTS ux_line_active ON line_bindings(line_user_id) WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS notification_log (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    task_id          INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    notice_type      TEXT NOT NULL,
    send_date        TEXT NOT NULL,
    status           TEXT NOT NULL,
    line_request_id  TEXT,
    error            TEXT,
    UNIQUE (task_id, notice_type, send_date)
);

-- Actions and IDs only; never document text or personal data.
CREATE TABLE IF NOT EXISTS audit_events (
    id           INTEGER PRIMARY KEY,
    actor_id     INTEGER REFERENCES accounts(id) ON DELETE SET NULL,
    case_id      INTEGER REFERENCES cases(id) ON DELETE SET NULL,
    entity_type  TEXT NOT NULL,
    entity_id    TEXT,
    action       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
