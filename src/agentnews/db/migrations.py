from collections.abc import Callable
from dataclasses import dataclass
import sqlite3

from .connection import transaction


MigrationFunction = Callable[[sqlite3.Connection], None]


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    apply: MigrationFunction


def _initial_schema(connection: sqlite3.Connection) -> None:
    for statement in (
        """
        CREATE TABLE user_preferences (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            display_name TEXT,
            email TEXT,
            timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
            topics_json TEXT NOT NULL DEFAULT '[]',
            keywords_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,

        """
        INSERT INTO user_preferences (id)
        VALUES (1)
        """,

        """
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
        """,

        """
        CREATE TABLE report_sources (
            id INTEGER PRIMARY KEY,
            report_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            url TEXT NOT NULL,
            source_name TEXT NOT NULL,
            published_at TEXT,
            snippet TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );
        """,

        """
        CREATE TABLE chat_sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """,

        """
        CREATE TABLE chat_messages (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
            content TEXT NOT NULL,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        );
        """,

        """
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
            completed_at TEXT,
            FOREIGN KEY (report_id) REFERENCES reports(id) ON DELETE CASCADE
        );
        """,

        """
        CREATE INDEX idx_reports_report_date
            ON reports(report_date);
        """,
        """
        CREATE INDEX idx_report_sources_report_id
            ON report_sources(report_id);
        """,
        """
        CREATE INDEX idx_chat_messages_session_created_at
            ON chat_messages(session_id, created_at);
        """,
        """
        CREATE INDEX idx_email_deliveries_report_status
            ON email_deliveries(report_id, status);
        """,
    ):
        connection.execute(statement)


def _add_report_source_archive_path(connection: sqlite3.Connection) -> None:
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(report_sources)").fetchall()
    }
    if "archive_file_path" not in columns:
        connection.execute(
            "ALTER TABLE report_sources ADD COLUMN archive_file_path TEXT"
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(version=1, name="initial_schema", apply=_initial_schema),
    Migration(
        version=2,
        name="report_source_archive_path",
        apply=_add_report_source_archive_path,
    ),
)


def _ensure_migrations_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )


def _validate_migrations(migrations: tuple[Migration, ...]) -> None:
    versions = [migration.version for migration in migrations]
    if versions != sorted(versions) or len(versions) != len(set(versions)):
        raise ValueError("migrations must have unique, ascending versions")
    if versions != list(range(1, len(versions) + 1)):
        raise ValueError("migration versions must be contiguous and start at 1")


def run_migrations(
    connection: sqlite3.Connection,
    migrations: tuple[Migration, ...] = MIGRATIONS,
) -> list[int]:
    """Apply pending migrations, one atomic transaction per migration."""

    _validate_migrations(migrations)
    _ensure_migrations_table(connection)
    applied_versions: list[int] = []
    known_migrations = {migration.version: migration.name for migration in migrations}
    applied_migrations = connection.execute(
        "SELECT version, name FROM schema_migrations ORDER BY version"
    ).fetchall()

    for applied in applied_migrations:
        known_name = known_migrations.get(applied["version"])
        if known_name is None:
            raise RuntimeError(
                "database schema is newer than this application; upgrade the application"
            )
        if known_name != applied["name"]:
            raise RuntimeError(
                f"migration {applied['version']} name does not match the migration history"
            )

    for migration in migrations:
        with transaction(connection, immediate=True):
            already_applied = connection.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if already_applied is not None:
                continue

            migration.apply(connection)
            connection.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (migration.version, migration.name),
            )
            applied_versions.append(migration.version)

    return applied_versions
