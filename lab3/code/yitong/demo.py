"""Scripted end-to-end demonstration for manual verification."""

from __future__ import annotations

from .app import Application
from .models import Permission
from .utils import format_money


def run_demo(database_path: str = ":memory:") -> dict[str, object]:
    """Run a complete happy path and return important identifiers."""
    app = Application.create(database_path)
    try:
        owner = app.auth.register(
            "demo_owner", "韦依彤", "Passw0rd!", phone="13800008801"
        )
        member = app.auth.register(
            "demo_member", "徐一帆", "Passw0rd!", phone="13900001234"
        )
        platform = app.auth.register(
            "demo_platform", "平台管理员", "Passw0rd!", platform_admin=True
        )
        ledger = app.ledger.create_ledger(owner.user_id, "家庭旅行账本")
        invitation = app.ledger.invite(owner.user_id, ledger.ledger_id, member.account)
        member_ledger = app.ledger.accept_invitation(
            member.user_id, invitation.invitation_id
        )
        app.ledger.set_permission(
            owner.user_id,
            ledger.ledger_id,
            member_ledger.member_id,
            Permission.AUDIT.value,
            True,
        )
        members = app.ledger.list_members(owner.user_id, ledger.ledger_id)
        owner_member = next(item for item in members if item.user_id == owner.user_id)
        member_member = next(item for item in members if item.user_id == member.user_id)
        record = app.records.add_record(
            actor_id=owner.user_id,
            ledger_id=ledger.ledger_id,
            record_type="EXPENSE",
            amount="120.00",
            category="餐饮",
            occurred_at="2026-11-20T12:00:00+08:00",
            note="午餐",
            payer_id=owner_member.member_id,
            shares={owner_member.member_id: 6000, member_member.member_id: 6000},
            request_id="demo-req-001",
        )
        app.records.approve_record(member.user_id, record.record_id)
        record2 = app.records.add_record(
            actor_id=member.user_id,
            ledger_id=ledger.ledger_id,
            record_type="EXPENSE",
            amount="80.00",
            category="交通",
            occurred_at="2026-11-20T18:00:00+08:00",
            note="打车",
            payer_id=member_member.member_id,
            shares={owner_member.member_id: 4000, member_member.member_id: 4000},
            request_id="demo-req-002",
        )
        app.records.approve_record(owner.user_id, record2.record_id)
        correction = app.records.create_correction(
            owner.user_id,
            record.record_id,
            "20.00",
            "INCREASE",
            "demo-cor-001",
            "补记饮料",
        )
        app.records.approve_correction(member.user_id, correction.correction_id)
        stats = app.records.statistics(owner.user_id, ledger.ledger_id)
        settlement = app.settlements.start(owner.user_id, ledger.ledger_id)
        app.settlements.confirm(owner.user_id, settlement.settlement_id)
        final_status = app.settlements.confirm(member.user_id, settlement.settlement_id)
        logs = app.audit.list_for_ledger(owner.user_id, ledger.ledger_id)
        return {
            "owner_id": owner.user_id,
            "member_id": member.user_id,
            "platform_id": platform.user_id,
            "ledger_id": ledger.ledger_id,
            "record_ids": [record.record_id, record2.record_id],
            "correction_id": correction.correction_id,
            "settlement_id": settlement.settlement_id,
            "settlement_status": final_status["settlement"].status,
            "snapshot": final_status["snapshot"] is not None,
            "expense_total": format_money(stats["expense_cents"]),
            "log_count": len(logs),
        }
    finally:
        app.close()
