-- AgentNews SQLite schema
--
-- 在空库上完整执行 src/agentnews/db/migrations.py 之后导出的建表 SQL，用于阅读与评审。
-- 权威来源始终是 migrations.py：应用启动时按版本号执行尚未应用的迁移，
-- 并写入 schema_migrations；请勿在已有数据库上直接执行本文件。
--
-- 已应用迁移: 1, 2, 3, 4, 5, 6
-- 对象数量: 19

-- table: chat_messages
CREATE TABLE chat_messages (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

-- table: chat_sessions
CREATE TABLE chat_sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')), archived_at TEXT);

-- table: email_deliveries
CREATE TABLE email_deliveries (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL,
    recipient TEXT NOT NULL,
    subject TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    requested_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT, trigger_source TEXT NOT NULL DEFAULT 'manual_api' CHECK (trigger_source IN ('manual_api', 'natural_language', 'scheduled_task')), idempotency_key TEXT, error_code TEXT,
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
);

-- table: email_delivery_attempts
CREATE TABLE email_delivery_attempts (
    id INTEGER PRIMARY KEY,
    delivery_id INTEGER NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    status TEXT NOT NULL CHECK (status IN ('sent', 'failed')),
    error_code TEXT,
    error_message TEXT,
    attempted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (delivery_id, attempt_number),
    FOREIGN KEY (delivery_id) REFERENCES email_deliveries(id) ON DELETE CASCADE
);

-- table: report_sources
CREATE TABLE report_sources (
    id INTEGER PRIMARY KEY,
    report_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    source_name TEXT NOT NULL,
    published_at TEXT,
    snippet TEXT NOT NULL DEFAULT '', archive_file_path TEXT,
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
);

-- table: reports
CREATE TABLE reports (
    id INTEGER PRIMARY KEY,
    report_date TEXT NOT NULL,
    title TEXT NOT NULL,
    file_path TEXT NOT NULL UNIQUE,
    summary TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- table: scheduled_digest_runs
CREATE TABLE scheduled_digest_runs (
    id INTEGER PRIMARY KEY,
    business_date TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')),
    attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0 AND attempt_count <= 3),
    report_id INTEGER,
    email_delivery_id INTEGER,
    started_at TEXT,
    completed_at TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE SET NULL,
    FOREIGN KEY (email_delivery_id) REFERENCES email_deliveries(id) ON DELETE SET NULL
);

-- table: schema_migrations
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- table: user_preferences
CREATE TABLE user_preferences (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    display_name TEXT,
    email TEXT,
    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
    topics_json TEXT NOT NULL DEFAULT '[]',
    keywords_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
, daily_send_enabled INTEGER NOT NULL DEFAULT 0 CHECK (daily_send_enabled IN (0, 1)), daily_send_time TEXT NOT NULL DEFAULT '08:00' CHECK (daily_send_time GLOB '[0-2][0-9]:[0-5][0-9]' AND substr(daily_send_time, 1, 2) < '24'));

-- index: idx_chat_messages_session_created_at
CREATE INDEX idx_chat_messages_session_created_at
ON chat_messages(session_id, created_at);

-- index: idx_chat_sessions_status_updated_at
CREATE INDEX idx_chat_sessions_status_updated_at
ON chat_sessions(status, updated_at DESC);

-- index: idx_email_deliveries_idempotency_key
CREATE UNIQUE INDEX idx_email_deliveries_idempotency_key
ON email_deliveries(idempotency_key)
WHERE idempotency_key IS NOT NULL;

-- index: idx_email_deliveries_report_requested
CREATE INDEX idx_email_deliveries_report_requested
ON email_deliveries(report_id, requested_at DESC, id DESC);

-- index: idx_email_deliveries_report_status
CREATE INDEX idx_email_deliveries_report_status
ON email_deliveries(report_id, status);

-- index: idx_email_delivery_attempts_delivery
CREATE INDEX idx_email_delivery_attempts_delivery ON email_delivery_attempts(delivery_id, attempt_number);

-- index: idx_report_sources_report_id
CREATE INDEX idx_report_sources_report_id
ON report_sources(report_id);

-- index: idx_reports_report_date
CREATE INDEX idx_reports_report_date
ON reports(report_date);

-- index: idx_scheduled_digest_runs_date
CREATE INDEX idx_scheduled_digest_runs_date ON scheduled_digest_runs(business_date DESC);

-- index: idx_scheduled_digest_runs_status
CREATE INDEX idx_scheduled_digest_runs_status ON scheduled_digest_runs(status, business_date DESC);
