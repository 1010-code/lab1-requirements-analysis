"""SQLite persistence for the 一同记账 prototype."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    account TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    phone TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    password_salt TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_admins (
    user_id TEXT PRIMARY KEY,
    granted_at TEXT NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(user_id)
);
CREATE TABLE IF NOT EXISTS ledgers (
    ledger_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (owner_id) REFERENCES users(user_id)
);

CREATE TABLE IF NOT EXISTS ledger_members (
    member_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    permissions_json TEXT NOT NULL,
    status TEXT NOT NULL,
    permission_version INTEGER NOT NULL DEFAULT 1,
    joined_at TEXT NOT NULL,
    left_at TEXT,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    FOREIGN KEY (user_id) REFERENCES users(user_id),
    UNIQUE (ledger_id, user_id)
);

CREATE TABLE IF NOT EXISTS invitations (
    invitation_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    invitee_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    responded_at TEXT,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    FOREIGN KEY (inviter_id) REFERENCES users(user_id),
    FOREIGN KEY (invitee_id) REFERENCES users(user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_invitation
ON invitations(ledger_id, invitee_id)
WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS expense_records (
    record_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    record_type TEXT NOT NULL,
    category TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    payer_id TEXT NOT NULL,
    submitter_id TEXT NOT NULL,
    auditor_id TEXT,
    status TEXT NOT NULL,
    request_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    FOREIGN KEY (payer_id) REFERENCES ledger_members(member_id),
    FOREIGN KEY (submitter_id) REFERENCES users(user_id),
    FOREIGN KEY (auditor_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS ix_record_ledger_status
ON expense_records(ledger_id, status, occurred_at);
CREATE INDEX IF NOT EXISTS ix_record_duplicate
ON expense_records(ledger_id, submitter_id, amount_cents, record_type, category, occurred_at);

CREATE TABLE IF NOT EXISTS record_shares (
    share_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    FOREIGN KEY (record_id) REFERENCES expense_records(record_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES ledger_members(member_id),
    UNIQUE (record_id, member_id)
);

CREATE TABLE IF NOT EXISTS attachments (
    attachment_id TEXT PRIMARY KEY,
    record_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_type TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (record_id) REFERENCES expense_records(record_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS correction_records (
    correction_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    original_record_id TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    direction TEXT NOT NULL,
    status TEXT NOT NULL,
    submitter_id TEXT NOT NULL,
    auditor_id TEXT,
    request_id TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    FOREIGN KEY (original_record_id) REFERENCES expense_records(record_id),
    FOREIGN KEY (submitter_id) REFERENCES users(user_id),
    FOREIGN KEY (auditor_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS ix_correction_original
ON correction_records(original_record_id, status);

CREATE TABLE IF NOT EXISTS budgets (
    budget_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    category TEXT NOT NULL,
    cycle_type TEXT NOT NULL,
    limit_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    UNIQUE (ledger_id, category, cycle_type)
);

CREATE TABLE IF NOT EXISTS settlements (
    settlement_id TEXT PRIMARY KEY,
    ledger_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    total_cents INTEGER NOT NULL,
    snapshot_at TEXT,
    revoked_at TEXT,
    FOREIGN KEY (ledger_id) REFERENCES ledgers(ledger_id),
    FOREIGN KEY (created_by) REFERENCES users(user_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_pending_settlement
ON settlements(ledger_id)
WHERE status = 'PENDING';

CREATE TABLE IF NOT EXISTS settlement_confirmations (
    confirmation_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_at TEXT,
    FOREIGN KEY (settlement_id) REFERENCES settlements(settlement_id) ON DELETE CASCADE,
    FOREIGN KEY (member_id) REFERENCES ledger_members(member_id),
    UNIQUE (settlement_id, member_id)
);

CREATE TABLE IF NOT EXISTS settlement_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    settlement_id TEXT NOT NULL UNIQUE,
    summary_json TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    frozen_at TEXT NOT NULL,
    FOREIGN KEY (settlement_id) REFERENCES settlements(settlement_id)
);

CREATE TABLE IF NOT EXISTS operation_logs (
    log_id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL,
    ledger_id TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '{}',
    after_json TEXT NOT NULL DEFAULT '{}',
    result TEXT NOT NULL,
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    FOREIGN KEY (actor_id) REFERENCES users(user_id)
);
CREATE INDEX IF NOT EXISTS ix_log_ledger_created
ON operation_logs(ledger_id, created_at);
CREATE INDEX IF NOT EXISTS ix_log_actor_created
ON operation_logs(actor_id, created_at);

CREATE TABLE IF NOT EXISTS request_keys (
    request_id TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    result_ref TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (actor_id, request_id),
    FOREIGN KEY (actor_id) REFERENCES users(user_id)
);
"""


class Database:
    """Small SQLite wrapper that keeps one reusable connection."""

    def __init__(self, path: str | Path = "yitong.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self._transaction_depth = 0

    def initialize(self) -> None:
        """Create tables and indexes if they do not exist."""
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a group of statements atomically."""
        self._transaction_depth += 1
        try:
            yield self.connection
            if self._transaction_depth == 1:
                self.connection.commit()
        except Exception:
            if self._transaction_depth == 1:
                self.connection.rollback()
            raise
        finally:
            self._transaction_depth -= 1

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        """Execute one statement and commit outside an explicit transaction."""
        cursor = self.connection.execute(sql, params)
        if self._transaction_depth == 0:
            self.connection.commit()
        return cursor

    def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        """Fetch one row."""
        return self.connection.execute(sql, params).fetchone()

    def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        """Fetch all rows."""
        return self.connection.execute(sql, params).fetchall()

    def close(self) -> None:
        """Close the database connection."""
        self.connection.close()

    def reset(self) -> None:
        """Drop all tables in development and test environments."""
        tables = self.fetchall(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
        self.connection.execute("PRAGMA foreign_keys = OFF")
        for row in tables:
            self.connection.execute(f'DROP TABLE IF EXISTS "{row["name"]}"')
        self.connection.commit()
        self.connection.execute("PRAGMA foreign_keys = ON")
