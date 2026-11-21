"""Typed data objects mirroring the core UML class model."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class UserStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    FROZEN = "FROZEN"


class LedgerRole(str, Enum):
    LEDGER_ADMIN = "LEDGER_ADMIN"
    MEMBER = "MEMBER"
    PLATFORM_ADMIN = "PLATFORM_ADMIN"


class Permission(str, Enum):
    BOOKKEEPING = "BOOKKEEPING"
    VIEW_ALL_RECORDS = "VIEW_ALL_RECORDS"
    VIEW_OWN_RECORDS = "VIEW_OWN_RECORDS"
    AUDIT = "AUDIT"
    VIEW_OWN_LOGS = "VIEW_OWN_LOGS"


DEFAULT_MEMBER_PERMISSIONS = {
    Permission.BOOKKEEPING.value,
    Permission.VIEW_OWN_RECORDS.value,
}


class RecordStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    VOIDED = "VOIDED"


class RecordType(str, Enum):
    EXPENSE = "EXPENSE"
    INCOME = "INCOME"


class AdjustmentDirection(str, Enum):
    INCREASE = "INCREASE"
    DECREASE = "DECREASE"


class SettlementStatus(str, Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class InvitationStatus(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass
class User:
    user_id: str
    account: str
    display_name: str
    password_hash: str
    password_salt: str
    status: str
    created_at: str
    phone: str = ""


@dataclass
class Ledger:
    ledger_id: str
    name: str
    owner_id: str
    status: str
    created_at: str


@dataclass
class LedgerMember:
    member_id: str
    ledger_id: str
    user_id: str
    role: str
    permissions: set[str]
    status: str
    permission_version: int
    joined_at: str
    left_at: str | None = None


@dataclass
class Invitation:
    invitation_id: str
    ledger_id: str
    inviter_id: str
    invitee_id: str
    status: str
    created_at: str
    responded_at: str | None = None


@dataclass
class RecordShare:
    share_id: str
    record_id: str
    member_id: str
    amount_cents: int


@dataclass
class Attachment:
    file_name: str
    file_type: str
    file_size: int
    content_hash: str = ""


@dataclass
class ExpenseRecord:
    record_id: str
    ledger_id: str
    amount_cents: int
    record_type: str
    category: str
    occurred_at: str
    note: str
    payer_id: str
    submitter_id: str
    auditor_id: str | None
    status: str
    request_id: str
    created_at: str
    updated_at: str
    version: int
    shares: list[RecordShare] = field(default_factory=list)
    attachments: list[Attachment] = field(default_factory=list)


@dataclass
class CorrectionRecord:
    correction_id: str
    ledger_id: str
    original_record_id: str
    amount_cents: int
    direction: str
    status: str
    submitter_id: str
    auditor_id: str | None
    request_id: str
    note: str
    occurred_at: str
    created_at: str
    updated_at: str
    version: int


@dataclass
class Budget:
    budget_id: str
    ledger_id: str
    category: str
    cycle_type: str
    limit_cents: int
    created_at: str


@dataclass
class Settlement:
    settlement_id: str
    ledger_id: str
    status: str
    created_by: str
    created_at: str
    total_cents: int
    snapshot_at: str | None = None
    revoked_at: str | None = None


@dataclass
class SettlementConfirmation:
    confirmation_id: str
    settlement_id: str
    member_id: str
    status: str
    created_at: str
    confirmed_at: str | None = None


@dataclass
class SettlementSnapshot:
    snapshot_id: str
    settlement_id: str
    summary_json: str
    detail_json: str
    frozen_at: str


@dataclass
class OperationLog:
    log_id: str
    actor_id: str
    ledger_id: str
    object_type: str
    object_id: str
    action: str
    before_json: str
    after_json: str
    result: str
    failure_reason: str
    created_at: str


@dataclass
class AuditEvent:
    actor_id: str
    ledger_id: str
    object_type: str
    object_id: str
    action: str
    before: Any = None
    after: Any = None
    result: str = "SUCCESS"
    failure_reason: str = ""
