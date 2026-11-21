"""Unit tests for 一同记账 services."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from yitong.app import Application
from yitong.errors import (
    AuthenticationError,
    AuthorizationError,
    ConflictError,
    SuspiciousDuplicateError,
    ValidationError,
)
from yitong.models import Permission


class LedgerServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.app = Application.create(":memory:")
        self.owner = self.app.auth.register(
            "owner", "韦依彤", "Passw0rd!", phone="13800008801"
        )
        self.member = self.app.auth.register(
            "member", "徐一帆", "Passw0rd!", phone="13900001234"
        )
        self.outsider = self.app.auth.register("outsider", "林晓", "Passw0rd!")
        self.platform = self.app.auth.register(
            "platform", "平台管理员", "Passw0rd!", platform_admin=True
        )
        self.ledger = self.app.ledger.create_ledger(self.owner.user_id, "旅行账本")
        invite = self.app.ledger.invite(
            self.owner.user_id, self.ledger.ledger_id, self.member.account
        )
        self.member_membership = self.app.ledger.accept_invitation(
            self.member.user_id, invite.invitation_id
        )
        self.owner_membership = next(
            item
            for item in self.app.ledger.list_members(
                self.owner.user_id, self.ledger.ledger_id
            )
            if item.user_id == self.owner.user_id
        )
        self.app.ledger.set_permission(
            self.owner.user_id,
            self.ledger.ledger_id,
            self.member_membership.member_id,
            Permission.AUDIT.value,
            True,
        )

    def tearDown(self) -> None:
        self.app.close()

    def _record(
        self,
        user=None,
        request="req-1",
        amount="120.00",
        category="餐饮",
        occurred="2026-11-20T12:00:00+08:00",
    ):
        actor = user or self.owner
        return self.app.records.add_record(
            actor.user_id,
            self.ledger.ledger_id,
            "EXPENSE",
            amount,
            category,
            occurred,
            "测试记录",
            self.owner_membership.member_id,
            {
                self.owner_membership.member_id: 6000,
                self.member_membership.member_id: 6000,
            },
            request,
        )

    def test_login_and_invalid_password(self) -> None:
        logged = self.app.auth.login("OWNER", "Passw0rd!")
        self.assertEqual(logged.user_id, self.owner.user_id)
        with self.assertRaises(AuthenticationError):
            self.app.auth.login("owner", "wrong-pass1")

    def test_default_member_permissions_and_invitation(self) -> None:
        permissions = self.app.ledger.member_permissions(
            self.member.user_id, self.ledger.ledger_id
        )
        self.assertIn(Permission.BOOKKEEPING.value, permissions)
        self.assertIn(Permission.AUDIT.value, permissions)
        self.assertNotIn(Permission.VIEW_ALL_RECORDS.value, permissions)

    def test_amount_validation_and_share_total(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.records.add_record(
                self.owner.user_id,
                self.ledger.ledger_id,
                "EXPENSE",
                "0",
                "餐饮",
                "2026-11-20T12:00:00+08:00",
                "",
                self.owner_membership.member_id,
                {self.owner_membership.member_id: 100},
                "bad-zero",
            )
        with self.assertRaises(ValidationError):
            self.app.records.add_record(
                self.owner.user_id,
                self.ledger.ledger_id,
                "EXPENSE",
                "10.00",
                "餐饮",
                "2026-11-20T12:00:00+08:00",
                "",
                self.owner_membership.member_id,
                {self.owner_membership.member_id: 999},
                "bad-shares",
            )

    def test_idempotent_request_returns_same_record(self) -> None:
        first = self._record(request="same-request")
        second = self._record(request="same-request")
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(
            len(
                self.app.records.query_records(
                    self.owner.user_id, self.ledger.ledger_id
                )
            ),
            1,
        )

    def test_suspicious_duplicate_requires_confirmation(self) -> None:
        self._record(request="duplicate-1")
        with self.assertRaises(SuspiciousDuplicateError):
            self._record(request="duplicate-2")

    def test_self_audit_rejected_and_other_auditor_allowed(self) -> None:
        record = self._record()
        with self.assertRaises(AuthorizationError):
            self.app.records.approve_record(self.owner.user_id, record.record_id)
        approved = self.app.records.approve_record(
            self.member.user_id, record.record_id
        )
        self.assertEqual(approved.status, "APPROVED")

    def test_correction_and_statistics(self) -> None:
        record = self._record()
        self.app.records.approve_record(self.member.user_id, record.record_id)
        correction = self.app.records.create_correction(
            self.owner.user_id,
            record.record_id,
            "20.00",
            "INCREASE",
            "correction-1",
            "补记",
        )
        self.app.records.approve_correction(
            self.member.user_id, correction.correction_id
        )
        stats = self.app.records.statistics(self.owner.user_id, self.ledger.ledger_id)
        self.assertEqual(stats["expense_cents"], 14000)

    def test_settlement_snapshot_and_export(self) -> None:
        record = self._record()
        self.app.records.approve_record(self.member.user_id, record.record_id)
        settlement = self.app.settlements.start(
            self.owner.user_id, self.ledger.ledger_id
        )
        self.app.settlements.confirm(self.owner.user_id, settlement.settlement_id)
        final = self.app.settlements.confirm(
            self.member.user_id, settlement.settlement_id
        )
        self.assertEqual(final["settlement"].status, "COMPLETED")
        self.assertIsNotNone(final["snapshot"])
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.app.reporting.export_settlement_csv(
                self.owner.user_id,
                settlement.settlement_id,
                Path(temp_dir) / "settlement.csv",
            )
            self.assertTrue(path.exists())
            self.assertIn("结算差额", path.read_text(encoding="utf-8-sig"))

    def test_permission_and_log_scope(self) -> None:
        record = self._record()
        self.app.records.approve_record(self.member.user_id, record.record_id)
        with self.assertRaises(AuthorizationError):
            self.app.audit.list_for_ledger(
                self.member.user_id, self.ledger.ledger_id, scope="own"
            )
        self.app.ledger.set_permission(
            self.owner.user_id,
            self.ledger.ledger_id,
            self.member_membership.member_id,
            Permission.VIEW_OWN_LOGS.value,
            True,
        )
        logs = self.app.audit.list_for_ledger(
            self.member.user_id, self.ledger.ledger_id, scope="own"
        )
        self.assertTrue(all(item.actor_id == self.member.user_id for item in logs))
        with self.assertRaises(AuthorizationError):
            self.app.audit.list_for_ledger(self.outsider.user_id, self.ledger.ledger_id)

    def test_platform_admin_can_change_account_status(self) -> None:
        overview = self.app.platform.overview(self.platform.user_id)
        self.assertGreaterEqual(overview["users"], 4)
        self.app.platform.set_account_status(
            self.platform.user_id, self.outsider.user_id, "SUSPENDED"
        )
        with self.assertRaises(AuthenticationError):
            self.app.auth.login("outsider", "Passw0rd!")

    def test_revoke_settlement_keeps_snapshot(self) -> None:
        record = self._record()
        self.app.records.approve_record(self.member.user_id, record.record_id)
        settlement = self.app.settlements.start(
            self.owner.user_id, self.ledger.ledger_id
        )
        self.app.settlements.confirm(self.owner.user_id, settlement.settlement_id)
        final = self.app.settlements.confirm(
            self.member.user_id, settlement.settlement_id
        )
        self.assertIsNotNone(final["snapshot"])
        cancelled = self.app.settlements.revoke(
            self.owner.user_id, settlement.settlement_id
        )
        self.assertEqual(cancelled.status, "CANCELLED")
        self.assertIsNotNone(
            self.app.settlements.snapshot(self.owner.user_id, settlement.settlement_id)
        )

    def test_duplicate_settlement_creation_conflicts(self) -> None:
        settlement = self.app.settlements.start(
            self.owner.user_id, self.ledger.ledger_id
        )
        self.assertEqual(settlement.status, "PENDING")
        with self.assertRaises(ConflictError):
            self.app.settlements.start(self.owner.user_id, self.ledger.ledger_id)


if __name__ == "__main__":
    unittest.main()
