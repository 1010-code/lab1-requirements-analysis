"""CSV export and permission-aware reporting helpers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from .auth_service import AuditService
from .database import Database
from .errors import AuthorizationError, NotFoundError
from .ledger_service import LedgerService
from .models import AuditEvent, LedgerRole, Permission
from .record_service import RecordService
from .repositories import MemberRepository, SettlementRepository, UserRepository
from .utils import format_money, mask_phone


class ReportingService:
    """Builds exportable statistics and settlement files."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.ledger = LedgerService(db)
        self.records = RecordService(db)
        self.members = MemberRepository(db)
        self.users = UserRepository(db)
        self.settlements = SettlementRepository(db)
        self.audit = AuditService(db)

    def export_statistics_csv(
        self,
        actor_id: str,
        ledger_id: str,
        output_path: str | Path,
        category: str | None = None,
        member_id: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
    ) -> Path:
        """Export a member's visible statistics or an administrator's report."""
        effective = self.ledger.member_permissions(actor_id, ledger_id)
        if Permission.VIEW_ALL_RECORDS.value not in effective:
            member = self.members.by_user(ledger_id, actor_id)
            if member is None:
                raise AuthorizationError("当前用户没有统计导出权限")
            member_id = member.member_id
        stats = self.records.statistics(
            actor_id,
            ledger_id,
            category=category,
            member_id=member_id,
            start_at=start_at,
            end_at=end_at,
        )
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["统计项目", "金额/状态"])
            writer.writerow(["支出合计", format_money(stats["expense_cents"])])
            writer.writerow(["收入合计", format_money(stats["income_cents"])])
            for key, value in sorted(stats["category_totals"].items()):
                writer.writerow([f"分类：{key}", format_money(value)])
            for key, value in sorted(stats["member_totals"].items()):
                member = self.members.get(key)
                user = self.users.get(member.user_id) if member else None
                writer.writerow(
                    [f"成员：{user.display_name if user else key}", format_money(value)]
                )
            for budget in stats["budgets"]:
                writer.writerow(
                    [
                        f"预算：{budget['category']}",
                        f"{format_money(budget['used_cents'])}/{format_money(budget['limit_cents'])}",
                    ]
                )
        self.audit.record(
            AuditEvent(
                actor_id=actor_id,
                ledger_id=ledger_id,
                object_type="export",
                object_id=str(path),
                action="EXPORT_STATISTICS",
                after={"path": str(path), "category": category, "member_id": member_id},
            )
        )
        return path

    def export_settlement_csv(
        self, actor_id: str, settlement_id: str, output_path: str | Path
    ) -> Path:
        """Export a settlement list; only ledger admins may do this."""
        settlement = self.settlements.get(settlement_id)
        if settlement is None:
            raise NotFoundError("结算不存在")
        member = self.ledger._require_active_member(actor_id, settlement.ledger_id)
        if member.role != LedgerRole.LEDGER_ADMIN.value:
            raise AuthorizationError("结算名单只有账本管理员可以导出")
        snapshot = self.settlements.snapshot(settlement_id)
        if snapshot is None:
            raise NotFoundError("结算快照尚未生成")
        detail = json.loads(snapshot.detail_json)
        summary = json.loads(snapshot.summary_json)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["成员", "手机号", "结算差额"])
            balances = summary.get("balances", {})
            for member_id, cents in balances.items():
                ledger_member = self.members.get(member_id)
                user = self.users.get(ledger_member.user_id) if ledger_member else None
                writer.writerow(
                    [
                        user.display_name if user else member_id,
                        mask_phone(getattr(user, "phone", "") if user else ""),
                        format_money(cents),
                    ]
                )
            writer.writerow([])
            writer.writerow(["明细"])
            for item in detail:
                writer.writerow([json.dumps(item, ensure_ascii=False)])
        self.audit.record(
            AuditEvent(
                actor_id=actor_id,
                ledger_id=settlement.ledger_id,
                object_type="export",
                object_id=str(path),
                action="EXPORT_SETTLEMENT",
                after={"settlement_id": settlement_id, "path": str(path)},
            )
        )
        return path

    def export_logs_csv(
        self, actor_id: str, ledger_id: str, output_path: str | Path, scope: str = "all"
    ) -> Path:
        """Export logs visible to the current user."""
        logs = self.audit.list_for_ledger(actor_id, ledger_id, scope=scope)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["时间", "操作人", "操作", "对象", "结果", "失败原因"])
            for item in logs:
                writer.writerow(
                    [
                        item.created_at,
                        item.actor_id,
                        item.action,
                        f"{item.object_type}:{item.object_id}",
                        item.result,
                        item.failure_reason,
                    ]
                )
        return path

    def ledger_summary(self, actor_id: str, ledger_id: str) -> dict[str, Any]:
        """Return a compact summary for CLI and future UI adapters."""
        stats = self.records.statistics(actor_id, ledger_id)
        members = self.members.list_for_ledger(ledger_id)
        return {
            "ledger_id": ledger_id,
            "member_count": len([item for item in members if item.status == "ACTIVE"]),
            "stats": stats,
        }
