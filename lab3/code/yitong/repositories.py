"""Repository layer for persistence and query composition."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .database import Database
from .models import (
    Attachment,
    Budget,
    CorrectionRecord,
    ExpenseRecord,
    Invitation,
    Ledger,
    LedgerMember,
    OperationLog,
    RecordShare,
    Settlement,
    SettlementConfirmation,
    SettlementSnapshot,
    User,
)


def _user(row: sqlite3.Row | None) -> User | None:
    if row is None:
        return None
    return User(
        user_id=row["user_id"],
        account=row["account"],
        display_name=row["display_name"],
        password_hash=row["password_hash"],
        password_salt=row["password_salt"],
        status=row["status"],
        created_at=row["created_at"],
        phone=row["phone"],
    )


def _ledger(row: sqlite3.Row | None) -> Ledger | None:
    if row is None:
        return None
    return Ledger(
        ledger_id=row["ledger_id"],
        name=row["name"],
        owner_id=row["owner_id"],
        status=row["status"],
        created_at=row["created_at"],
    )


def _member(row: sqlite3.Row | None) -> LedgerMember | None:
    if row is None:
        return None
    return LedgerMember(
        member_id=row["member_id"],
        ledger_id=row["ledger_id"],
        user_id=row["user_id"],
        role=row["role"],
        permissions=set(json.loads(row["permissions_json"])),
        status=row["status"],
        permission_version=row["permission_version"],
        joined_at=row["joined_at"],
        left_at=row["left_at"],
    )


def _invitation(row: sqlite3.Row | None) -> Invitation | None:
    if row is None:
        return None
    return Invitation(
        invitation_id=row["invitation_id"],
        ledger_id=row["ledger_id"],
        inviter_id=row["inviter_id"],
        invitee_id=row["invitee_id"],
        status=row["status"],
        created_at=row["created_at"],
        responded_at=row["responded_at"],
    )


def _record(row: sqlite3.Row | None) -> ExpenseRecord | None:
    if row is None:
        return None
    return ExpenseRecord(
        record_id=row["record_id"],
        ledger_id=row["ledger_id"],
        amount_cents=row["amount_cents"],
        record_type=row["record_type"],
        category=row["category"],
        occurred_at=row["occurred_at"],
        note=row["note"],
        payer_id=row["payer_id"],
        submitter_id=row["submitter_id"],
        auditor_id=row["auditor_id"],
        status=row["status"],
        request_id=row["request_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


def _correction(row: sqlite3.Row | None) -> CorrectionRecord | None:
    if row is None:
        return None
    return CorrectionRecord(
        correction_id=row["correction_id"],
        ledger_id=row["ledger_id"],
        original_record_id=row["original_record_id"],
        amount_cents=row["amount_cents"],
        direction=row["direction"],
        status=row["status"],
        submitter_id=row["submitter_id"],
        auditor_id=row["auditor_id"],
        request_id=row["request_id"],
        note=row["note"],
        occurred_at=row["occurred_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
    )


def _budget(row: sqlite3.Row | None) -> Budget | None:
    if row is None:
        return None
    return Budget(
        budget_id=row["budget_id"],
        ledger_id=row["ledger_id"],
        category=row["category"],
        cycle_type=row["cycle_type"],
        limit_cents=row["limit_cents"],
        created_at=row["created_at"],
    )


def _settlement(row: sqlite3.Row | None) -> Settlement | None:
    if row is None:
        return None
    return Settlement(
        settlement_id=row["settlement_id"],
        ledger_id=row["ledger_id"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        total_cents=row["total_cents"],
        snapshot_at=row["snapshot_at"],
        revoked_at=row["revoked_at"],
    )


class UserRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, user: User) -> None:
        self.db.execute(
            "INSERT INTO users(user_id, account, display_name, password_hash, password_salt, status, created_at, phone) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                user.user_id,
                user.account,
                user.display_name,
                user.password_hash,
                user.password_salt,
                user.status,
                user.created_at,
                user.phone,
            ),
        )

    def get(self, user_id: str) -> User | None:
        return _user(
            self.db.fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
        )

    def by_account(self, account: str) -> User | None:
        return _user(
            self.db.fetchone("SELECT * FROM users WHERE account = ?", (account,))
        )

    def set_status(self, user_id: str, status: str) -> None:
        self.db.execute(
            "UPDATE users SET status = ? WHERE user_id = ?", (status, user_id)
        )

    def list_all(self) -> list[User]:
        rows = self.db.fetchall("SELECT * FROM users ORDER BY created_at DESC")
        return [item for row in rows if (item := _user(row)) is not None]


class LedgerRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, ledger: Ledger) -> None:
        self.db.execute(
            "INSERT INTO ledgers(ledger_id, name, owner_id, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                ledger.ledger_id,
                ledger.name,
                ledger.owner_id,
                ledger.status,
                ledger.created_at,
            ),
        )

    def get(self, ledger_id: str) -> Ledger | None:
        return _ledger(
            self.db.fetchone("SELECT * FROM ledgers WHERE ledger_id = ?", (ledger_id,))
        )

    def update_owner(self, ledger_id: str, owner_id: str) -> None:
        self.db.execute(
            "UPDATE ledgers SET owner_id = ? WHERE ledger_id = ?", (owner_id, ledger_id)
        )

    def list_for_user(self, user_id: str) -> list[Ledger]:
        rows = self.db.fetchall(
            "SELECT l.* FROM ledgers l JOIN ledger_members m ON m.ledger_id = l.ledger_id WHERE m.user_id = ? AND m.status = 'ACTIVE' ORDER BY l.created_at",
            (user_id,),
        )
        return [item for row in rows if (item := _ledger(row)) is not None]


class MemberRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, member: LedgerMember) -> None:
        self.db.execute(
            "INSERT INTO ledger_members(member_id, ledger_id, user_id, role, permissions_json, status, permission_version, joined_at, left_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                member.member_id,
                member.ledger_id,
                member.user_id,
                member.role,
                json.dumps(sorted(member.permissions), ensure_ascii=False),
                member.status,
                member.permission_version,
                member.joined_at,
                member.left_at,
            ),
        )

    def get(self, member_id: str) -> LedgerMember | None:
        return _member(
            self.db.fetchone(
                "SELECT * FROM ledger_members WHERE member_id = ?", (member_id,)
            )
        )

    def by_user(self, ledger_id: str, user_id: str) -> LedgerMember | None:
        return _member(
            self.db.fetchone(
                "SELECT * FROM ledger_members WHERE ledger_id = ? AND user_id = ?",
                (ledger_id, user_id),
            )
        )

    def list_for_ledger(self, ledger_id: str) -> list[LedgerMember]:
        rows = self.db.fetchall(
            "SELECT * FROM ledger_members WHERE ledger_id = ? ORDER BY joined_at",
            (ledger_id,),
        )
        return [item for row in rows if (item := _member(row)) is not None]

    def count_active_admins(self, ledger_id: str) -> int:
        row = self.db.fetchone(
            "SELECT COUNT(*) AS total FROM ledger_members WHERE ledger_id = ? AND role = 'LEDGER_ADMIN' AND status = 'ACTIVE'",
            (ledger_id,),
        )
        return int(row["total"]) if row else 0

    def update_role(self, member_id: str, role: str) -> None:
        self.db.execute(
            "UPDATE ledger_members SET role = ?, permission_version = permission_version + 1 WHERE member_id = ?",
            (role, member_id),
        )

    def update_permissions(self, member_id: str, permissions: set[str]) -> None:
        self.db.execute(
            "UPDATE ledger_members SET permissions_json = ?, permission_version = permission_version + 1 WHERE member_id = ?",
            (json.dumps(sorted(permissions), ensure_ascii=False), member_id),
        )

    def deactivate(self, member_id: str, left_at: str) -> None:
        self.db.execute(
            "UPDATE ledger_members SET status = 'LEFT', left_at = ?, permission_version = permission_version + 1 WHERE member_id = ?",
            (left_at, member_id),
        )


class InvitationRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, invitation: Invitation) -> None:
        self.db.execute(
            "INSERT INTO invitations(invitation_id, ledger_id, inviter_id, invitee_id, status, created_at, responded_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                invitation.invitation_id,
                invitation.ledger_id,
                invitation.inviter_id,
                invitation.invitee_id,
                invitation.status,
                invitation.created_at,
                invitation.responded_at,
            ),
        )

    def get(self, invitation_id: str) -> Invitation | None:
        return _invitation(
            self.db.fetchone(
                "SELECT * FROM invitations WHERE invitation_id = ?", (invitation_id,)
            )
        )

    def pending_between(self, ledger_id: str, invitee_id: str) -> Invitation | None:
        return _invitation(
            self.db.fetchone(
                "SELECT * FROM invitations WHERE ledger_id = ? AND invitee_id = ? AND status = 'PENDING'",
                (ledger_id, invitee_id),
            )
        )

    def set_status(self, invitation_id: str, status: str, responded_at: str) -> None:
        self.db.execute(
            "UPDATE invitations SET status = ?, responded_at = ? WHERE invitation_id = ?",
            (status, responded_at, invitation_id),
        )

    def list_for_user(self, user_id: str) -> list[Invitation]:
        rows = self.db.fetchall(
            "SELECT * FROM invitations WHERE invitee_id = ? ORDER BY created_at DESC",
            (user_id,),
        )
        return [item for row in rows if (item := _invitation(row)) is not None]


class RecordRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, record: ExpenseRecord) -> None:
        self.db.execute(
            "INSERT INTO expense_records(record_id, ledger_id, amount_cents, record_type, category, occurred_at, note, payer_id, submitter_id, auditor_id, status, request_id, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.record_id,
                record.ledger_id,
                record.amount_cents,
                record.record_type,
                record.category,
                record.occurred_at,
                record.note,
                record.payer_id,
                record.submitter_id,
                record.auditor_id,
                record.status,
                record.request_id,
                record.created_at,
                record.updated_at,
                record.version,
            ),
        )

    def get(self, record_id: str) -> ExpenseRecord | None:
        return _record(
            self.db.fetchone(
                "SELECT * FROM expense_records WHERE record_id = ?", (record_id,)
            )
        )

    def add_share(self, share: RecordShare) -> None:
        self.db.execute(
            "INSERT INTO record_shares(share_id, record_id, member_id, amount_cents) VALUES (?, ?, ?, ?)",
            (share.share_id, share.record_id, share.member_id, share.amount_cents),
        )

    def shares(self, record_id: str) -> list[RecordShare]:
        rows = self.db.fetchall(
            "SELECT * FROM record_shares WHERE record_id = ? ORDER BY share_id",
            (record_id,),
        )
        return [
            RecordShare(
                share_id=row["share_id"],
                record_id=row["record_id"],
                member_id=row["member_id"],
                amount_cents=row["amount_cents"],
            )
            for row in rows
        ]

    def add_attachment(self, record_id: str, attachment: Attachment) -> None:
        from .utils import new_id

        self.db.execute(
            "INSERT INTO attachments(attachment_id, record_id, file_name, file_type, file_size, content_hash) VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_id("att"),
                record_id,
                attachment.file_name,
                attachment.file_type,
                attachment.file_size,
                attachment.content_hash,
            ),
        )

    def attachments(self, record_id: str) -> list[Attachment]:
        rows = self.db.fetchall(
            "SELECT * FROM attachments WHERE record_id = ? ORDER BY attachment_id",
            (record_id,),
        )
        return [
            Attachment(
                file_name=row["file_name"],
                file_type=row["file_type"],
                file_size=row["file_size"],
                content_hash=row["content_hash"],
            )
            for row in rows
        ]

    def update(
        self,
        record_id: str,
        fields: dict[str, Any],
        expected_version: int | None = None,
    ) -> int:
        from .utils import utc_now_iso

        allowed = {
            "amount_cents",
            "record_type",
            "category",
            "occurred_at",
            "note",
            "payer_id",
            "auditor_id",
            "status",
        }
        assignments: list[str] = []
        params: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                raise ValueError(f"unsupported record field: {key}")
            assignments.append(f"{key} = ?")
            params.append(value)
        assignments.append("version = version + 1")
        sql = (
            "UPDATE expense_records SET "
            + ", ".join(assignments)
            + ", updated_at = ? WHERE record_id = ?"
        )
        params.extend([utc_now_iso(), record_id])
        if expected_version is not None:
            sql += " AND version = ?"
            params.append(expected_version)
        return self.db.execute(sql, tuple(params)).rowcount

    def replace_shares(self, record_id: str, shares: Iterable[RecordShare]) -> None:
        self.db.execute("DELETE FROM record_shares WHERE record_id = ?", (record_id,))
        for share in shares:
            self.add_share(share)

    def list(
        self,
        ledger_id: str,
        category: str | None = None,
        member_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        amount_min: int | None = None,
        amount_max: int | None = None,
        status: str | None = None,
        record_type: str | None = None,
        keyword: str | None = None,
        submitter_id: str | None = None,
    ) -> list[ExpenseRecord]:
        clauses = ["r.ledger_id = ?"]
        params: list[Any] = [ledger_id]
        for column, value in (
            ("r.category", category),
            ("r.status", status),
            ("r.record_type", record_type),
            ("r.submitter_id", submitter_id),
        ):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if start_at:
            clauses.append("r.occurred_at >= ?")
            params.append(start_at)
        if end_at:
            clauses.append("r.occurred_at <= ?")
            params.append(end_at)
        if amount_min is not None:
            clauses.append("r.amount_cents >= ?")
            params.append(amount_min)
        if amount_max is not None:
            clauses.append("r.amount_cents <= ?")
            params.append(amount_max)
        if keyword:
            clauses.append("(r.note LIKE ? OR r.category LIKE ?)")
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        if member_id:
            clauses.append(
                "EXISTS (SELECT 1 FROM record_shares s WHERE s.record_id = r.record_id AND s.member_id = ?)"
            )
            params.append(member_id)
        rows = self.db.fetchall(
            "SELECT r.* FROM expense_records r WHERE "
            + " AND ".join(clauses)
            + " ORDER BY r.occurred_at DESC, r.created_at DESC",
            tuple(params),
        )
        return [item for row in rows if (item := _record(row)) is not None]

    def list_approved(self, ledger_id: str) -> list[ExpenseRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM expense_records WHERE ledger_id = ? AND status = 'APPROVED' ORDER BY occurred_at",
            (ledger_id,),
        )
        return [item for row in rows if (item := _record(row)) is not None]

    def suspicious(
        self,
        ledger_id: str,
        submitter_id: str,
        amount_cents: int,
        record_type: str,
        category: str,
        occurred_at: str,
    ) -> ExpenseRecord | None:
        return _record(
            self.db.fetchone(
                "SELECT * FROM expense_records WHERE ledger_id = ? AND submitter_id = ? AND amount_cents = ? AND record_type = ? AND category = ? AND occurred_at = ? AND status != 'VOIDED' ORDER BY created_at LIMIT 1",
                (
                    ledger_id,
                    submitter_id,
                    amount_cents,
                    record_type,
                    category,
                    occurred_at,
                ),
            )
        )

    def create_correction(self, item: CorrectionRecord) -> None:
        self.db.execute(
            "INSERT INTO correction_records(correction_id, ledger_id, original_record_id, amount_cents, direction, status, submitter_id, auditor_id, request_id, note, occurred_at, created_at, updated_at, version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.correction_id,
                item.ledger_id,
                item.original_record_id,
                item.amount_cents,
                item.direction,
                item.status,
                item.submitter_id,
                item.auditor_id,
                item.request_id,
                item.note,
                item.occurred_at,
                item.created_at,
                item.updated_at,
                item.version,
            ),
        )

    def correction(self, correction_id: str) -> CorrectionRecord | None:
        return _correction(
            self.db.fetchone(
                "SELECT * FROM correction_records WHERE correction_id = ?",
                (correction_id,),
            )
        )

    def corrections_for_ledger(self, ledger_id: str) -> list[CorrectionRecord]:
        rows = self.db.fetchall(
            "SELECT * FROM correction_records WHERE ledger_id = ? ORDER BY created_at",
            (ledger_id,),
        )
        return [item for row in rows if (item := _correction(row)) is not None]

    def update_correction(
        self,
        correction_id: str,
        status: str,
        auditor_id: str | None,
        expected_version: int | None = None,
    ) -> int:
        from .utils import utc_now_iso

        sql = "UPDATE correction_records SET status = ?, auditor_id = ?, version = version + 1, updated_at = ? WHERE correction_id = ?"
        params: list[Any] = [status, auditor_id, utc_now_iso(), correction_id]
        if expected_version is not None:
            sql += " AND version = ?"
            params.append(expected_version)
        return self.db.execute(sql, tuple(params)).rowcount


class BudgetRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, budget: Budget) -> None:
        self.db.execute(
            "INSERT INTO budgets(budget_id, ledger_id, category, cycle_type, limit_cents, created_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(ledger_id, category, cycle_type) DO UPDATE SET limit_cents = excluded.limit_cents, created_at = excluded.created_at",
            (
                budget.budget_id,
                budget.ledger_id,
                budget.category,
                budget.cycle_type,
                budget.limit_cents,
                budget.created_at,
            ),
        )

    def list_for_ledger(self, ledger_id: str) -> list[Budget]:
        rows = self.db.fetchall(
            "SELECT * FROM budgets WHERE ledger_id = ? ORDER BY category, cycle_type",
            (ledger_id,),
        )
        return [item for row in rows if (item := _budget(row)) is not None]


class SettlementRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, settlement: Settlement) -> None:
        self.db.execute(
            "INSERT INTO settlements(settlement_id, ledger_id, status, created_by, created_at, total_cents, snapshot_at, revoked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                settlement.settlement_id,
                settlement.ledger_id,
                settlement.status,
                settlement.created_by,
                settlement.created_at,
                settlement.total_cents,
                settlement.snapshot_at,
                settlement.revoked_at,
            ),
        )

    def get(self, settlement_id: str) -> Settlement | None:
        return _settlement(
            self.db.fetchone(
                "SELECT * FROM settlements WHERE settlement_id = ?", (settlement_id,)
            )
        )

    def active_for_ledger(self, ledger_id: str) -> Settlement | None:
        return _settlement(
            self.db.fetchone(
                "SELECT * FROM settlements WHERE ledger_id = ? AND status = 'PENDING'",
                (ledger_id,),
            )
        )

    def add_confirmation(self, item: SettlementConfirmation) -> None:
        self.db.execute(
            "INSERT INTO settlement_confirmations(confirmation_id, settlement_id, member_id, status, created_at, confirmed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                item.confirmation_id,
                item.settlement_id,
                item.member_id,
                item.status,
                item.created_at,
                item.confirmed_at,
            ),
        )

    def confirmations(self, settlement_id: str) -> list[SettlementConfirmation]:
        rows = self.db.fetchall(
            "SELECT * FROM settlement_confirmations WHERE settlement_id = ? ORDER BY created_at",
            (settlement_id,),
        )
        return [
            SettlementConfirmation(
                confirmation_id=row["confirmation_id"],
                settlement_id=row["settlement_id"],
                member_id=row["member_id"],
                status=row["status"],
                created_at=row["created_at"],
                confirmed_at=row["confirmed_at"],
            )
            for row in rows
        ]

    def confirm(self, settlement_id: str, member_id: str, at: str) -> int:
        return self.db.execute(
            "UPDATE settlement_confirmations SET status = 'CONFIRMED', confirmed_at = ? WHERE settlement_id = ? AND member_id = ? AND status = 'PENDING'",
            (at, settlement_id, member_id),
        ).rowcount

    def update_status(
        self,
        settlement_id: str,
        status: str,
        snapshot_at: str | None = None,
        revoked_at: str | None = None,
    ) -> None:
        self.db.execute(
            "UPDATE settlements SET status = ?, snapshot_at = ?, revoked_at = ? WHERE settlement_id = ?",
            (status, snapshot_at, revoked_at, settlement_id),
        )

    def create_snapshot(self, snapshot: SettlementSnapshot) -> None:
        self.db.execute(
            "INSERT INTO settlement_snapshots(snapshot_id, settlement_id, summary_json, detail_json, frozen_at) VALUES (?, ?, ?, ?, ?)",
            (
                snapshot.snapshot_id,
                snapshot.settlement_id,
                snapshot.summary_json,
                snapshot.detail_json,
                snapshot.frozen_at,
            ),
        )

    def snapshot(self, settlement_id: str) -> SettlementSnapshot | None:
        row = self.db.fetchone(
            "SELECT * FROM settlement_snapshots WHERE settlement_id = ?",
            (settlement_id,),
        )
        if row is None:
            return None
        return SettlementSnapshot(
            snapshot_id=row["snapshot_id"],
            settlement_id=row["settlement_id"],
            summary_json=row["summary_json"],
            detail_json=row["detail_json"],
            frozen_at=row["frozen_at"],
        )

    def list_for_ledger(self, ledger_id: str) -> list[Settlement]:
        rows = self.db.fetchall(
            "SELECT * FROM settlements WHERE ledger_id = ? ORDER BY created_at DESC",
            (ledger_id,),
        )
        return [item for row in rows if (item := _settlement(row)) is not None]


class RequestKeyRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, actor_id: str, request_id: str) -> sqlite3.Row | None:
        return self.db.fetchone(
            "SELECT * FROM request_keys WHERE actor_id = ? AND request_id = ?",
            (actor_id, request_id),
        )

    def upsert(
        self,
        actor_id: str,
        request_id: str,
        action: str,
        result_ref: str,
        created_at: str,
    ) -> None:
        self.db.execute(
            "INSERT INTO request_keys(request_id, actor_id, action, result_ref, created_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT(actor_id, request_id) DO UPDATE SET action = excluded.action, result_ref = excluded.result_ref, created_at = excluded.created_at",
            (request_id, actor_id, action, result_ref, created_at),
        )


class LogRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, item: OperationLog) -> None:
        self.db.execute(
            "INSERT INTO operation_logs(log_id, actor_id, ledger_id, object_type, object_id, action, before_json, after_json, result, failure_reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.log_id,
                item.actor_id,
                item.ledger_id,
                item.object_type,
                item.object_id,
                item.action,
                item.before_json,
                item.after_json,
                item.result,
                item.failure_reason,
                item.created_at,
            ),
        )

    def list(
        self,
        ledger_id: str,
        actor_id: str | None = None,
        action: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> list[OperationLog]:
        clauses = ["ledger_id = ?"]
        params: list[Any] = [ledger_id]
        for column, value in (("actor_id", actor_id), ("action", action)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        if start_at:
            clauses.append("created_at >= ?")
            params.append(start_at)
        if end_at:
            clauses.append("created_at <= ?")
            params.append(end_at)
        rows = self.db.fetchall(
            "SELECT * FROM operation_logs WHERE "
            + " AND ".join(clauses)
            + " ORDER BY created_at DESC",
            tuple(params),
        )
        return [
            OperationLog(
                log_id=row["log_id"],
                actor_id=row["actor_id"],
                ledger_id=row["ledger_id"],
                object_type=row["object_type"],
                object_id=row["object_id"],
                action=row["action"],
                before_json=row["before_json"],
                after_json=row["after_json"],
                result=row["result"],
                failure_reason=row["failure_reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]
