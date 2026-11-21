"""Settlement, confirmation, snapshot, and cancellation services."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .auth_service import AuditService
from .database import Database
from .errors import ConflictError, InvalidStateError, NotFoundError
from .ledger_service import LedgerService
from .models import (
    AdjustmentDirection,
    AuditEvent,
    RecordStatus,
    Settlement,
    SettlementConfirmation,
    SettlementSnapshot,
    SettlementStatus,
)
from .repositories import MemberRepository, RecordRepository, SettlementRepository
from .utils import format_money, new_id, utc_now_iso


class SettlementService:
    """Builds settlements from approved records and freezes snapshots."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.ledger = LedgerService(db)
        self.audit = AuditService(db)
        self.records = RecordRepository(db)
        self.members = MemberRepository(db)
        self.settlements = SettlementRepository(db)

    def _build_balances(
        self, ledger_id: str
    ) -> tuple[dict[str, int], list[dict[str, Any]], int]:
        paid: dict[str, int] = defaultdict(int)
        owed: dict[str, int] = defaultdict(int)
        details: list[dict[str, Any]] = []
        total = 0
        for record in self.records.list_approved(ledger_id):
            if record.record_type != "EXPENSE":
                continue
            shares = self.records.shares(record.record_id)
            paid[record.payer_id] += record.amount_cents
            for share in shares:
                owed[share.member_id] += share.amount_cents
            total += record.amount_cents
            details.append(
                {
                    "record_id": record.record_id,
                    "amount_cents": record.amount_cents,
                    "category": record.category,
                    "payer_id": record.payer_id,
                    "shares": [
                        {
                            "member_id": share.member_id,
                            "amount_cents": share.amount_cents,
                        }
                        for share in shares
                    ],
                }
            )
        for correction in self.records.corrections_for_ledger(ledger_id):
            if correction.status != RecordStatus.APPROVED.value:
                continue
            original = self.records.get(correction.original_record_id)
            if (
                original is None
                or original.status != "APPROVED"
                or original.record_type != "EXPENSE"
            ):
                continue
            sign = (
                1 if correction.direction == AdjustmentDirection.INCREASE.value else -1
            )
            delta = sign * correction.amount_cents
            shares = self.records.shares(original.record_id)
            paid[original.payer_id] += delta
            total_shares = sum(share.amount_cents for share in shares)
            allocated = 0
            for index, share in enumerate(shares):
                if total_shares == 0:
                    break
                if index == len(shares) - 1:
                    part = delta - allocated
                else:
                    part = int(delta * share.amount_cents / total_shares)
                    allocated += part
                owed[share.member_id] += part
            total += delta
            details.append(
                {
                    "correction_id": correction.correction_id,
                    "original_record_id": original.record_id,
                    "amount_cents": delta,
                    "direction": correction.direction,
                }
            )
        balances = {}
        for member in self.members.list_for_ledger(ledger_id):
            if member.status == "ACTIVE":
                balances[member.member_id] = (
                    paid[member.member_id] - owed[member.member_id]
                )
        return balances, details, total

    def start(self, actor_id: str, ledger_id: str) -> Settlement:
        """Start one pending settlement and create member confirmations."""
        self.ledger._require_admin(actor_id, ledger_id)
        if self.settlements.active_for_ledger(ledger_id):
            raise ConflictError("当前账本已有待确认结算")
        balances, details, total = self._build_balances(ledger_id)
        now = utc_now_iso()
        settlement = Settlement(
            settlement_id=new_id("set"),
            ledger_id=ledger_id,
            status=SettlementStatus.PENDING.value,
            created_by=actor_id,
            created_at=now,
            total_cents=total,
        )
        with self.db.transaction():
            self.settlements.create(settlement)
            for member in self.members.list_for_ledger(ledger_id):
                if member.status == "ACTIVE":
                    self.settlements.add_confirmation(
                        SettlementConfirmation(
                            confirmation_id=new_id("cfm"),
                            settlement_id=settlement.settlement_id,
                            member_id=member.member_id,
                            status="PENDING",
                            created_at=now,
                        )
                    )
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="settlement",
                    object_id=settlement.settlement_id,
                    action="START_SETTLEMENT",
                    after={
                        "total": format_money(total),
                        "balances": {
                            key: format_money(value) for key, value in balances.items()
                        },
                        "details": details,
                    },
                )
            )
        return settlement

    def confirm(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """Confirm an actor's part; complete when every member confirmed."""
        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise NotFoundError("结算不存在")
        member = self.ledger._require_active_member(actor_id, settlement.ledger_id)
        if settlement.status != SettlementStatus.PENDING.value:
            raise InvalidStateError("当前结算不在待确认状态")
        with self.db.transaction():
            changed = self.settlements.confirm(
                settlement_id, member.member_id, utc_now_iso()
            )
            if changed != 1:
                raise ConflictError("该成员已经确认或确认状态已变化")
            confirmations = self.settlements.confirmations(settlement_id)
            if all(item.status == "CONFIRMED" for item in confirmations):
                balances, details, total = self._build_balances(settlement.ledger_id)
                frozen_at = utc_now_iso()
                summary = {
                    "settlement_id": settlement_id,
                    "total_cents": total,
                    "balances": balances,
                    "member_count": len(confirmations),
                }
                self.settlements.create_snapshot(
                    SettlementSnapshot(
                        snapshot_id=new_id("snap"),
                        settlement_id=settlement_id,
                        summary_json=json.dumps(summary, ensure_ascii=False),
                        detail_json=json.dumps(details, ensure_ascii=False),
                        frozen_at=frozen_at,
                    )
                )
                self.settlements.update_status(
                    settlement_id,
                    SettlementStatus.COMPLETED.value,
                    snapshot_at=frozen_at,
                )
                self.audit.record(
                    AuditEvent(
                        actor_id=actor_id,
                        ledger_id=settlement.ledger_id,
                        object_type="settlement",
                        object_id=settlement_id,
                        action="COMPLETE_SETTLEMENT",
                        after={"snapshot_at": frozen_at},
                    )
                )
            else:
                self.audit.record(
                    AuditEvent(
                        actor_id=actor_id,
                        ledger_id=settlement.ledger_id,
                        object_type="settlement",
                        object_id=settlement_id,
                        action="CONFIRM_SETTLEMENT",
                    )
                )
        return self.status(actor_id, settlement_id)

    def status(self, actor_id: str, settlement_id: str) -> dict[str, Any]:
        """Return settlement, confirmation, and snapshot state."""
        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise NotFoundError("结算不存在")
        self.ledger._require_active_member(actor_id, settlement.ledger_id)
        return {
            "settlement": settlement,
            "confirmations": self.settlements.confirmations(settlement_id),
            "snapshot": self.settlements.snapshot(settlement_id),
        }

    def revoke(self, actor_id: str, settlement_id: str) -> Settlement:
        """Cancel a settlement while retaining any generated snapshot."""
        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise NotFoundError("结算不存在")
        self.ledger._require_admin(actor_id, settlement.ledger_id)
        if settlement.status == SettlementStatus.CANCELLED.value:
            raise InvalidStateError("结算已经撤销")
        now = utc_now_iso()
        with self.db.transaction():
            self.settlements.update_status(
                settlement_id,
                SettlementStatus.CANCELLED.value,
                snapshot_at=settlement.snapshot_at,
                revoked_at=now,
            )
            self.db.execute(
                "UPDATE settlement_confirmations SET status = 'CANCELLED' WHERE settlement_id = ?",
                (settlement_id,),
            )
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=settlement.ledger_id,
                    object_type="settlement",
                    object_id=settlement_id,
                    action="REVOKE_SETTLEMENT",
                    before={"status": settlement.status},
                    after={"status": SettlementStatus.CANCELLED.value},
                )
            )
        refreshed = self.settlements.get(settlement_id)
        if refreshed is None:
            raise NotFoundError("结算不存在")
        return refreshed

    def list(self, actor_id: str, ledger_id: str) -> list[Settlement]:
        """List settlements for an authorized member."""
        self.ledger._require_active_member(actor_id, ledger_id)
        return self.settlements.list_for_ledger(ledger_id)

    def snapshot(self, actor_id: str, settlement_id: str) -> SettlementSnapshot:
        """Return a frozen snapshot after access checks."""
        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise NotFoundError("结算不存在")
        self.ledger._require_active_member(actor_id, settlement.ledger_id)
        item = self.settlements.snapshot(settlement_id)
        if item is None:
            raise NotFoundError("快照尚未生成")
        return item
