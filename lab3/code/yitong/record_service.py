"""Expense records, review, correction, budgets, and statistics."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from .auth_service import AuditService
from .database import Database
from .errors import (
    AuthorizationError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
    SuspiciousDuplicateError,
    ValidationError,
)
from .ledger_service import LedgerService
from .models import (
    AdjustmentDirection,
    Attachment,
    AuditEvent,
    Budget,
    CorrectionRecord,
    ExpenseRecord,
    LedgerRole,
    Permission,
    RecordShare,
    RecordStatus,
    RecordType,
)
from .repositories import (
    BudgetRepository,
    MemberRepository,
    RecordRepository,
    RequestKeyRepository,
)
from .utils import (
    format_money,
    is_within_hours,
    new_id,
    normalize_request_id,
    parse_datetime,
    parse_money,
    utc_now_iso,
    validate_attachments,
)


class RecordService:
    """Coordinates record validation, state transitions, and reporting."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.ledger = LedgerService(db)
        self.audit = AuditService(db)
        self.records = RecordRepository(db)
        self.members = MemberRepository(db)
        self.budgets = BudgetRepository(db)
        self.request_keys = RequestKeyRepository(db)

    def _replay_record(self, actor_id: str, request_id: str) -> ExpenseRecord | None:
        key = self.request_keys.get(actor_id, request_id)
        if key and is_within_hours(key["created_at"]):
            if key["action"] != "ADD_RECORD":
                raise ConflictError("该请求编号已经用于其他操作")
            return self.records.get(key["result_ref"])
        return None

    def _validate_share_members(self, ledger_id: str, shares: dict[str, int]) -> None:
        if not shares:
            raise ValidationError("至少需要一个承担成员")
        for member_id, amount_cents in shares.items():
            if amount_cents <= 0:
                raise ValidationError("每名承担成员的金额必须大于 0")
            member = self.members.get(member_id)
            if (
                member is None
                or member.ledger_id != ledger_id
                or member.status != "ACTIVE"
            ):
                raise ValidationError("承担成员不属于当前账本或已退出")

    def _normalize_type(self, record_type: str) -> str:
        value = str(record_type).upper()
        if value not in {item.value for item in RecordType}:
            raise ValidationError("收支类型必须是 EXPENSE 或 INCOME")
        return value

    def _normalize_direction(self, direction: str) -> str:
        value = str(direction).upper()
        if value not in {item.value for item in AdjustmentDirection}:
            raise ValidationError("更正方向必须是 INCREASE 或 DECREASE")
        return value

    def add_record(
        self,
        actor_id: str,
        ledger_id: str,
        record_type: str,
        amount: str,
        category: str,
        occurred_at: str,
        note: str,
        payer_id: str,
        shares: dict[str, int],
        request_id: str,
        attachments: list[Attachment] | None = None,
        confirm_duplicate: bool = False,
    ) -> ExpenseRecord:
        """Add an expense or income record as PENDING."""
        self.ledger.require_permission(
            actor_id, ledger_id, Permission.BOOKKEEPING.value
        )
        self.ledger.ensure_no_pending_settlement(ledger_id, "新增收支")
        normalized_type = self._normalize_type(record_type)
        try:
            amount_cents = parse_money(amount)
            occurred = parse_datetime(occurred_at).isoformat()
            normalized_request = normalize_request_id(request_id)
            share_items = {member_id: int(value) for member_id, value in shares.items()}
            attachment_items = validate_attachments(attachments or [])
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        category_text = category.strip()
        if not category_text:
            raise ValidationError("类别不能为空")
        if sum(share_items.values()) != amount_cents:
            raise ValidationError("承担金额合计必须等于记录金额")
        self._validate_share_members(ledger_id, share_items)
        payer = self.members.get(payer_id)
        if payer is None or payer.ledger_id != ledger_id or payer.status != "ACTIVE":
            raise ValidationError("付款人不是当前账本成员")
        replay = self._replay_record(actor_id, normalized_request)
        if replay:
            return replay
        suspicious = self.records.suspicious(
            ledger_id, actor_id, amount_cents, normalized_type, category_text, occurred
        )
        if suspicious and not confirm_duplicate:
            raise SuspiciousDuplicateError(
                f"发现疑似重复记录 {suspicious.record_id}，确认后才允许提交"
            )
        now = utc_now_iso()
        record = ExpenseRecord(
            record_id=new_id("rec"),
            ledger_id=ledger_id,
            amount_cents=amount_cents,
            record_type=normalized_type,
            category=category_text,
            occurred_at=occurred,
            note=note.strip(),
            payer_id=payer_id,
            submitter_id=actor_id,
            auditor_id=None,
            status=RecordStatus.PENDING.value,
            request_id=normalized_request,
            created_at=now,
            updated_at=now,
            version=1,
        )
        with self.db.transaction():
            if not self.request_keys.claim(
                actor_id, normalized_request, "ADD_RECORD", record.record_id, now
            ):
                raise ConflictError("请求编号已处理，请刷新后查看原记录")
            self.records.create(record)
            for member_id, cents in share_items.items():
                self.records.add_share(
                    RecordShare(
                        share_id=new_id("shr"),
                        record_id=record.record_id,
                        member_id=member_id,
                        amount_cents=cents,
                    )
                )
            for item in attachment_items:
                self.records.add_attachment(record.record_id, item)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="record",
                    object_id=record.record_id,
                    action="ADD_RECORD",
                    after={
                        "amount": format_money(amount_cents),
                        "type": normalized_type,
                        "status": record.status,
                    },
                )
            )
        record.shares = self.records.shares(record.record_id)
        record.attachments = self.records.attachments(record.record_id)
        return record

    def get_record(self, actor_id: str, record_id: str) -> ExpenseRecord:
        """Load a record after checking view permission."""
        record = self.records.get(record_id)
        if record is None:
            raise NotFoundError("记录不存在")
        member = self.ledger.require_permission(
            actor_id, record.ledger_id, Permission.VIEW_OWN_RECORDS.value
        )
        effective = self.ledger.member_permissions(actor_id, record.ledger_id)
        shares = self.records.shares(record.record_id)
        own_share = any(item.member_id == member.member_id for item in shares)
        if (
            record.submitter_id != actor_id
            and not own_share
            and Permission.VIEW_ALL_RECORDS.value not in effective
        ):
            raise AuthorizationError("没有查看该记录的权限")
        record.shares = shares
        record.attachments = self.records.attachments(record.record_id)
        return record

    def _require_self_or_admin(
        self,
        actor_id: str,
        record: ExpenseRecord,
        permission: str = Permission.BOOKKEEPING.value,
    ) -> None:
        member = self.ledger.require_permission(actor_id, record.ledger_id, permission)
        if (
            record.submitter_id != actor_id
            and member.role != LedgerRole.LEDGER_ADMIN.value
        ):
            raise AuthorizationError("只能处理自己提交的记录")

    def modify_record(
        self,
        actor_id: str,
        record_id: str,
        fields: dict[str, Any],
        shares: dict[str, int] | None = None,
        expected_version: int | None = None,
    ) -> ExpenseRecord:
        """Modify a pending/rejected/withdrawn record and return it to PENDING."""
        original = self.records.get(record_id)
        if original is None:
            raise NotFoundError("记录不存在")
        self._require_self_or_admin(actor_id, original)
        self.ledger.ensure_no_pending_settlement(original.ledger_id, "修改收支记录")
        if original.status not in {
            RecordStatus.PENDING.value,
            RecordStatus.REJECTED.value,
            RecordStatus.WITHDRAWN.value,
        }:
            raise InvalidStateError("只有待审核、已驳回或已撤回记录可以修改")
        update: dict[str, Any] = {}
        if "amount" in fields:
            try:
                update["amount_cents"] = parse_money(fields["amount"])
            except ValueError as exc:
                raise ValidationError(str(exc)) from exc
        for key in ("record_type", "category", "occurred_at", "note", "payer_id"):
            if key in fields:
                value = fields[key]
                if key == "record_type":
                    value = self._normalize_type(value)
                elif key == "occurred_at":
                    try:
                        value = parse_datetime(value).isoformat()
                    except ValueError as exc:
                        raise ValidationError(str(exc)) from exc
                elif key == "category" and not str(value).strip():
                    raise ValidationError("类别不能为空")
                update[key] = value.strip() if isinstance(value, str) else value
        final_amount = update.get("amount_cents", original.amount_cents)
        if "amount_cents" in update and shares is None:
            raise ValidationError("修改金额时必须同时重新提交承担明细")
        if shares is not None:
            try:
                normalized_shares = {
                    member_id: int(amount_cents)
                    for member_id, amount_cents in shares.items()
                }
            except (TypeError, ValueError) as exc:
                raise ValidationError("承担金额必须是整数分") from exc
            self._validate_share_members(original.ledger_id, normalized_shares)
            if sum(normalized_shares.values()) != final_amount:
                raise ValidationError("承担金额合计必须等于记录金额")
            shares = normalized_shares
        update["status"] = RecordStatus.PENDING.value
        with self.db.transaction():
            affected = self.records.update(record_id, update, expected_version)
            if affected != 1:
                raise ConflictError("记录已被其他操作修改，请刷新后再试")
            if shares is not None:
                self.records.replace_shares(
                    record_id,
                    [
                        RecordShare(
                            share_id=new_id("shr"),
                            record_id=record_id,
                            member_id=member_id,
                            amount_cents=cents,
                        )
                        for member_id, cents in shares.items()
                    ],
                )
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=original.ledger_id,
                    object_type="record",
                    object_id=record_id,
                    action="MODIFY_RECORD",
                    before={"version": original.version, "status": original.status},
                    after={
                        "version": original.version + 1,
                        "status": RecordStatus.PENDING.value,
                    },
                )
            )
        return self.get_record(actor_id, record_id)

    def withdraw_record(
        self, actor_id: str, record_id: str, expected_version: int | None = None
    ) -> ExpenseRecord:
        """Withdraw a pending record without deleting it."""
        record = self.records.get(record_id)
        if record is None:
            raise NotFoundError("记录不存在")
        self._require_self_or_admin(actor_id, record)
        self.ledger.ensure_no_pending_settlement(record.ledger_id, "撤回收支记录")
        if record.status != RecordStatus.PENDING.value:
            raise InvalidStateError("只有待审核记录可以撤回")
        with self.db.transaction():
            if (
                self.records.update(
                    record_id,
                    {"status": RecordStatus.WITHDRAWN.value},
                    expected_version,
                )
                != 1
            ):
                raise ConflictError("记录状态已变化，请刷新")
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=record.ledger_id,
                    object_type="record",
                    object_id=record_id,
                    action="WITHDRAW_RECORD",
                    before={"status": record.status},
                    after={"status": RecordStatus.WITHDRAWN.value},
                )
            )
        return self.get_record(actor_id, record_id)

    def _review(
        self,
        actor_id: str,
        record_id: str,
        new_status: str,
        expected_version: int | None = None,
    ) -> ExpenseRecord:
        record = self.records.get(record_id)
        if record is None:
            raise NotFoundError("记录不存在")
        audit_member = self.ledger.require_permission(
            actor_id, record.ledger_id, Permission.AUDIT.value
        )
        self.ledger.ensure_no_pending_settlement(record.ledger_id, "审核收支记录")
        effective = self.ledger.member_permissions(actor_id, record.ledger_id)
        if Permission.VIEW_ALL_RECORDS.value not in effective:
            shares = self.records.shares(record.record_id)
            own_share = any(item.member_id == audit_member.member_id for item in shares)
            if record.submitter_id != actor_id and not own_share:
                raise AuthorizationError("审核人没有该记录的查看权限")
        if record.submitter_id == actor_id:
            raise AuthorizationError("审核人不能审核自己提交的记录")
        if record.status != RecordStatus.PENDING.value:
            raise InvalidStateError("只有待审核记录可以审核")
        with self.db.transaction():
            if (
                self.records.update(
                    record_id,
                    {"status": new_status, "auditor_id": actor_id},
                    expected_version,
                )
                != 1
            ):
                raise ConflictError("记录状态已变化，请刷新")
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=record.ledger_id,
                    object_type="record",
                    object_id=record_id,
                    action="REVIEW_RECORD",
                    before={"status": record.status},
                    after={"status": new_status, "auditor_id": actor_id},
                )
            )
        return self.get_record(actor_id, record_id)

    def approve_record(
        self, actor_id: str, record_id: str, expected_version: int | None = None
    ) -> ExpenseRecord:
        """Approve a pending record."""
        return self._review(
            actor_id, record_id, RecordStatus.APPROVED.value, expected_version
        )

    def reject_record(
        self, actor_id: str, record_id: str, expected_version: int | None = None
    ) -> ExpenseRecord:
        """Reject a pending record."""
        return self._review(
            actor_id, record_id, RecordStatus.REJECTED.value, expected_version
        )

    def void_record(
        self, actor_id: str, record_id: str, expected_version: int | None = None
    ) -> ExpenseRecord:
        """Void an approved record; the original row is retained."""
        record = self.records.get(record_id)
        if record is None:
            raise NotFoundError("记录不存在")
        self.ledger._require_admin(actor_id, record.ledger_id)
        self.ledger.ensure_no_pending_settlement(record.ledger_id, "作废收支记录")
        if record.status != RecordStatus.APPROVED.value:
            raise InvalidStateError("只有已审核记录可以作废")
        with self.db.transaction():
            if (
                self.records.update(
                    record_id, {"status": RecordStatus.VOIDED.value}, expected_version
                )
                != 1
            ):
                raise ConflictError("记录状态已变化，请刷新")
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=record.ledger_id,
                    object_type="record",
                    object_id=record_id,
                    action="VOID_RECORD",
                    before={"status": record.status},
                    after={"status": RecordStatus.VOIDED.value},
                )
            )
        return self.get_record(actor_id, record_id)

    def create_correction(
        self,
        actor_id: str,
        original_record_id: str,
        amount: str,
        direction: str,
        request_id: str,
        note: str = "",
        occurred_at: str | None = None,
    ) -> CorrectionRecord:
        """Create an auditable positive-amount correction with a direction."""
        original = self.records.get(original_record_id)
        if original is None:
            raise NotFoundError("原记录不存在")
        self.ledger.require_permission(
            actor_id, original.ledger_id, Permission.BOOKKEEPING.value
        )
        try:
            amount_cents = parse_money(amount)
            direction_value = self._normalize_direction(direction)
            request_value = normalize_request_id(request_id)
            occurred = parse_datetime(occurred_at or utc_now_iso()).isoformat()
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        key = self.request_keys.get(actor_id, request_value)
        if key and is_within_hours(key["created_at"]):
            if key["action"] == "ADD_CORRECTION":
                existing = self.records.correction(key["result_ref"])
                if existing:
                    return existing
            raise ConflictError("该请求编号已经处理")
        self.ledger.ensure_no_pending_settlement(original.ledger_id, "新增更正记录")
        if original.status != RecordStatus.APPROVED.value:
            raise InvalidStateError("只能对已审核记录发起更正")
        now = utc_now_iso()
        item = CorrectionRecord(
            correction_id=new_id("cor"),
            ledger_id=original.ledger_id,
            original_record_id=original_record_id,
            amount_cents=amount_cents,
            direction=direction_value,
            status=RecordStatus.PENDING.value,
            submitter_id=actor_id,
            auditor_id=None,
            request_id=request_value,
            note=note.strip(),
            occurred_at=occurred,
            created_at=now,
            updated_at=now,
            version=1,
        )
        with self.db.transaction():
            if not self.request_keys.claim(
                actor_id, request_value, "ADD_CORRECTION", item.correction_id, now
            ):
                raise ConflictError("请求编号已处理，请刷新后查看原更正记录")
            self.records.create_correction(item)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=original.ledger_id,
                    object_type="correction",
                    object_id=item.correction_id,
                    action="ADD_CORRECTION",
                    after={
                        "original": original_record_id,
                        "amount": format_money(amount_cents),
                        "direction": direction_value,
                    },
                )
            )
        return item

    def _review_correction(
        self,
        actor_id: str,
        correction_id: str,
        new_status: str,
        expected_version: int | None = None,
    ) -> CorrectionRecord:
        item = self.records.correction(correction_id)
        if item is None:
            raise NotFoundError("更正记录不存在")
        self.ledger.require_permission(actor_id, item.ledger_id, Permission.AUDIT.value)
        self.ledger.ensure_no_pending_settlement(item.ledger_id, "审核更正记录")
        if item.submitter_id == actor_id:
            raise AuthorizationError("审核人不能审核自己提交的更正记录")
        if item.status != RecordStatus.PENDING.value:
            raise InvalidStateError("只有待审核更正可以处理")
        with self.db.transaction():
            if (
                self.records.update_correction(
                    correction_id, new_status, actor_id, expected_version
                )
                != 1
            ):
                raise ConflictError("更正记录状态已变化，请刷新")
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=item.ledger_id,
                    object_type="correction",
                    object_id=correction_id,
                    action="REVIEW_CORRECTION",
                    before={"status": item.status},
                    after={"status": new_status},
                )
            )
        refreshed = self.records.correction(correction_id)
        if refreshed is None:
            raise NotFoundError("更正记录不存在")
        return refreshed

    def approve_correction(
        self, actor_id: str, correction_id: str, expected_version: int | None = None
    ) -> CorrectionRecord:
        """Approve a correction."""
        return self._review_correction(
            actor_id, correction_id, RecordStatus.APPROVED.value, expected_version
        )

    def reject_correction(
        self, actor_id: str, correction_id: str, expected_version: int | None = None
    ) -> CorrectionRecord:
        """Reject a correction."""
        return self._review_correction(
            actor_id, correction_id, RecordStatus.REJECTED.value, expected_version
        )

    def query_records(
        self,
        actor_id: str,
        ledger_id: str,
        category: str | None = None,
        member_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        amount_min: str | None = None,
        amount_max: str | None = None,
        status: str | None = None,
        record_type: str | None = None,
        keyword: str | None = None,
    ) -> list[ExpenseRecord]:
        """Query records and filter ordinary members' visible scope."""
        member = self.ledger.require_permission(
            actor_id, ledger_id, Permission.VIEW_OWN_RECORDS.value
        )
        effective = self.ledger.member_permissions(actor_id, ledger_id)
        if Permission.VIEW_ALL_RECORDS.value not in effective:
            member_id = member.member_id
        try:
            min_cents = parse_money(amount_min) if amount_min else None
            max_cents = parse_money(amount_max) if amount_max else None
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        rows = self.records.list(
            ledger_id,
            category,
            member_id,
            start_at,
            end_at,
            min_cents,
            max_cents,
            status,
            record_type,
            keyword,
        )
        if Permission.VIEW_ALL_RECORDS.value in effective:
            return rows
        visible = []
        for record in rows:
            shares = self.records.shares(record.record_id)
            if record.submitter_id == actor_id or any(
                item.member_id == member.member_id for item in shares
            ):
                record.shares = shares
                visible.append(record)
        return visible

    def _approved_effective_rows(
        self, ledger_id: str
    ) -> list[tuple[ExpenseRecord, list[RecordShare]]]:
        rows = self.records.list_approved(ledger_id)
        return [(record, self.records.shares(record.record_id)) for record in rows]

    def statistics(
        self,
        actor_id: str,
        ledger_id: str,
        category: str | None = None,
        member_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> dict[str, Any]:
        """Return approved totals, category totals, member totals, and budgets."""
        member = self.ledger.require_permission(
            actor_id, ledger_id, Permission.VIEW_OWN_RECORDS.value
        )
        effective = self.ledger.member_permissions(actor_id, ledger_id)
        view_all = Permission.VIEW_ALL_RECORDS.value in effective
        if not view_all:
            member_id = member.member_id
        records = self._approved_effective_rows(ledger_id)
        category_totals: dict[str, int] = defaultdict(int)
        member_totals: dict[str, int] = defaultdict(int)
        type_totals: dict[str, int] = defaultdict(int)
        total_expense = 0
        total_income = 0
        for record, shares in records:
            if category and record.category != category:
                continue
            if start_at and record.occurred_at < start_at:
                continue
            if end_at and record.occurred_at > end_at:
                continue
            visible_shares = [
                share
                for share in shares
                if not member_id or share.member_id == member_id
            ]
            scoped_amount = (
                record.amount_cents
                if view_all
                else sum(share.amount_cents for share in visible_shares)
            )
            if scoped_amount == 0:
                continue
            signed = (
                scoped_amount
                if record.record_type == RecordType.EXPENSE.value
                else -scoped_amount
            )
            if record.record_type == RecordType.EXPENSE.value:
                total_expense += scoped_amount
            else:
                total_income += scoped_amount
            category_totals[record.category] += signed
            type_totals[record.record_type] += scoped_amount
            for share in visible_shares:
                member_totals[share.member_id] += share.amount_cents
        for correction in self.records.corrections_for_ledger(ledger_id):
            if correction.status != RecordStatus.APPROVED.value:
                continue
            original = self.records.get(correction.original_record_id)
            if original is None or original.status != RecordStatus.APPROVED.value:
                continue
            if category and original.category != category:
                continue
            if start_at and correction.occurred_at < start_at:
                continue
            if end_at and correction.occurred_at > end_at:
                continue
            sign = (
                1 if correction.direction == AdjustmentDirection.INCREASE.value else -1
            )
            signed_delta = sign * correction.amount_cents
            shares = self.records.shares(original.record_id)
            total_share = sum(item.amount_cents for item in shares)
            parts: list[tuple[str, int]] = []
            if total_share:
                allocated = 0
                for index, share in enumerate(shares):
                    if index == len(shares) - 1:
                        part = signed_delta - allocated
                    else:
                        part = int(
                            Decimal(signed_delta)
                            * Decimal(share.amount_cents)
                            / Decimal(total_share)
                        )
                        allocated += part
                    if not member_id or share.member_id == member_id:
                        parts.append((share.member_id, part))
                for member_key, part in parts:
                    member_totals[member_key] += part
                scoped_delta = (
                    signed_delta if view_all else sum(part for _, part in parts)
                )
                if scoped_delta:
                    if original.record_type == RecordType.EXPENSE.value:
                        category_totals[original.category] += scoped_delta
                        total_expense += scoped_delta
                    else:
                        category_totals[original.category] -= scoped_delta
                        total_income += scoped_delta
                    type_totals[original.record_type] += scoped_delta
        budget_rows = []
        for budget in self.budgets.list_for_ledger(ledger_id):
            used = category_totals.get(budget.category, 0)
            budget_rows.append(
                {
                    "budget_id": budget.budget_id,
                    "category": budget.category,
                    "cycle_type": budget.cycle_type,
                    "limit_cents": budget.limit_cents,
                    "used_cents": used,
                    "over_budget": used > budget.limit_cents,
                }
            )
        return {
            "expense_cents": total_expense,
            "income_cents": total_income,
            "category_totals": dict(category_totals),
            "member_totals": dict(member_totals),
            "type_totals": dict(type_totals),
            "budgets": budget_rows,
        }

    def set_budget(
        self, actor_id: str, ledger_id: str, category: str, cycle_type: str, limit: str
    ) -> Budget:
        """Create or update a category budget."""
        self.ledger._require_admin(actor_id, ledger_id)
        if cycle_type.upper() not in {"MONTHLY", "CUSTOM"}:
            raise ValidationError("预算周期必须是 MONTHLY 或 CUSTOM")
        try:
            limit_cents = parse_money(limit)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        budget = Budget(
            budget_id=new_id("bud"),
            ledger_id=ledger_id,
            category=category.strip(),
            cycle_type=cycle_type.upper(),
            limit_cents=limit_cents,
            created_at=utc_now_iso(),
        )
        with self.db.transaction():
            self.budgets.upsert(budget)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="budget",
                    object_id=budget.budget_id,
                    action="SET_BUDGET",
                    after={
                        "category": budget.category,
                        "limit": format_money(limit_cents),
                    },
                )
            )
        return budget

    def list_budgets(self, actor_id: str, ledger_id: str) -> list[Budget]:
        """List budgets for authorized members."""
        self.ledger.require_permission(
            actor_id, ledger_id, Permission.VIEW_OWN_RECORDS.value
        )
        return self.budgets.list_for_ledger(ledger_id)
