"""Unit tests for 一同记账 services."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from yitong.app import Application
from yitong.cli import _jsonable, main
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

    def test_modify_amount_requires_new_shares(self) -> None:
        record = self._record(request="modify-amount")
        with self.assertRaises(ValidationError):
            self.app.records.modify_record(
                self.owner.user_id,
                record.record_id,
                {"amount": "200.00"},
            )
        updated = self.app.records.modify_record(
            self.owner.user_id,
            record.record_id,
            {"amount": "200.00"},
            shares={
                self.owner_membership.member_id: 10000,
                self.member_membership.member_id: 10000,
            },
        )
        self.assertEqual(updated.amount_cents, 20000)
        self.assertEqual(sum(item.amount_cents for item in updated.shares), 20000)

    def test_duplicate_correction_request_returns_first_result(self) -> None:
        record = self._record(request="correction-original")
        self.app.records.approve_record(self.member.user_id, record.record_id)
        first = self.app.records.create_correction(
            self.owner.user_id,
            record.record_id,
            "20.00",
            "INCREASE",
            "correction-repeat",
        )
        self.app.records.approve_correction(self.member.user_id, first.correction_id)
        self.app.settlements.start(self.owner.user_id, self.ledger.ledger_id)
        second = self.app.records.create_correction(
            self.owner.user_id,
            record.record_id,
            "20.00",
            "INCREASE",
            "correction-repeat",
        )
        self.assertEqual(first.correction_id, second.correction_id)
        count = self.app.db.fetchone("SELECT COUNT(*) AS n FROM correction_records")
        self.assertEqual(int(count["n"]), 1)

    def test_pending_settlement_freezes_record_and_member_changes(self) -> None:
        approved = self._record(request="freeze-approved")
        self.app.records.approve_record(self.member.user_id, approved.record_id)
        pending = self._record(
            request="freeze-pending",
            occurred="2026-11-20T12:10:00+08:00",
        )
        settlement = self.app.settlements.start(
            self.owner.user_id, self.ledger.ledger_id
        )
        self.assertEqual(settlement.status, "PENDING")
        with self.assertRaises(ConflictError):
            self.app.records.add_record(
                self.owner.user_id,
                self.ledger.ledger_id,
                "EXPENSE",
                "10.00",
                "餐饮",
                "2026-11-20T13:00:00+08:00",
                "",
                self.owner_membership.member_id,
                {self.owner_membership.member_id: 1000},
                "freeze-add",
            )
        with self.assertRaises(ConflictError):
            self.app.records.modify_record(
                self.owner.user_id,
                pending.record_id,
                {"note": "不能修改"},
            )
        with self.assertRaises(ConflictError):
            self.app.records.withdraw_record(self.owner.user_id, pending.record_id)
        with self.assertRaises(ConflictError):
            self.app.records.void_record(self.owner.user_id, approved.record_id)
        with self.assertRaises(ConflictError):
            self.app.ledger.invite(
                self.owner.user_id,
                self.ledger.ledger_id,
                self.outsider.account,
            )

    def test_negative_and_zero_shares_are_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.app.records.add_record(
                self.owner.user_id,
                self.ledger.ledger_id,
                "EXPENSE",
                "100.00",
                "餐饮",
                "2026-11-20T13:00:00+08:00",
                "",
                self.owner_membership.member_id,
                {
                    self.owner_membership.member_id: 12000,
                    self.member_membership.member_id: -2000,
                },
                "negative-share",
            )
        with self.assertRaises(ValidationError):
            self.app.records.add_record(
                self.owner.user_id,
                self.ledger.ledger_id,
                "EXPENSE",
                "100.00",
                "餐饮",
                "2026-11-20T13:10:00+08:00",
                "",
                self.owner_membership.member_id,
                {
                    self.owner_membership.member_id: 10000,
                    self.member_membership.member_id: 0,
                },
                "zero-share",
            )

    def test_auditor_without_record_scope_is_rejected(self) -> None:
        record = self.app.records.add_record(
            self.owner.user_id,
            self.ledger.ledger_id,
            "EXPENSE",
            "100.00",
            "餐饮",
            "2026-11-20T14:00:00+08:00",
            "仅管理员承担",
            self.owner_membership.member_id,
            {self.owner_membership.member_id: 10000},
            "audit-scope",
        )
        with self.assertRaises(AuthorizationError):
            self.app.records.approve_record(self.member.user_id, record.record_id)

    def test_left_member_can_be_reinvited_and_rejoin(self) -> None:
        self.app.ledger.exit_ledger(self.member.user_id, self.ledger.ledger_id)
        invite = self.app.ledger.invite(
            self.owner.user_id,
            self.ledger.ledger_id,
            self.member.account,
        )
        rejoined = self.app.ledger.accept_invitation(
            self.member.user_id, invite.invitation_id
        )
        self.assertEqual(rejoined.member_id, self.member_membership.member_id)
        self.assertEqual(rejoined.status, "ACTIVE")

    def test_income_correction_increase_and_decrease(self) -> None:
        income = self.app.records.add_record(
            self.owner.user_id,
            self.ledger.ledger_id,
            "INCOME",
            "100.00",
            "工资",
            "2026-11-20T15:00:00+08:00",
            "收入测试",
            self.owner_membership.member_id,
            {
                self.owner_membership.member_id: 5000,
                self.member_membership.member_id: 5000,
            },
            "income-original",
        )
        self.app.records.approve_record(self.member.user_id, income.record_id)
        increase = self.app.records.create_correction(
            self.owner.user_id,
            income.record_id,
            "20.00",
            "INCREASE",
            "income-increase",
        )
        self.app.records.approve_correction(self.member.user_id, increase.correction_id)
        stats = self.app.records.statistics(self.owner.user_id, self.ledger.ledger_id)
        self.assertEqual(stats["income_cents"], 12000)
        decrease = self.app.records.create_correction(
            self.owner.user_id,
            income.record_id,
            "20.00",
            "DECREASE",
            "income-decrease",
        )
        self.app.records.approve_correction(self.member.user_id, decrease.correction_id)
        stats = self.app.records.statistics(self.owner.user_id, self.ledger.ledger_id)
        self.assertEqual(stats["income_cents"], 10000)

    def test_session_token_authentication_and_logout(self) -> None:
        session = self.app.auth.create_session("owner", "Passw0rd!")
        self.assertEqual(
            set(session["user"]),
            {"user_id", "account", "display_name", "status"},
        )
        resolved = self.app.auth.authenticate_session(session["token"])
        self.assertEqual(resolved.user_id, self.owner.user_id)
        self.app.auth.revoke_session(session["token"])
        with self.assertRaises(AuthenticationError):
            self.app.auth.authenticate_session(session["token"])
        self.assertNotIn("password_hash", json.dumps(_jsonable(self.owner)))

    def test_cli_login_output_and_token_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "cli.db"
            register_output = io.StringIO()
            with contextlib.redirect_stdout(register_output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(database),
                            "register",
                            "--account",
                            "cli_user",
                            "--name",
                            "命令行用户",
                            "--password",
                            "Passw0rd!",
                        ]
                    ),
                    0,
                )
            login_output = io.StringIO()
            with contextlib.redirect_stdout(login_output):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(database),
                            "login",
                            "--account",
                            "cli_user",
                            "--password",
                            "Passw0rd!",
                        ]
                    ),
                    0,
                )
            register_payload = json.loads(register_output.getvalue())
            self.assertEqual(
                set(register_payload),
                {"user_id", "account", "display_name", "status"},
            )
            self.assertNotIn("password_hash", register_output.getvalue())
            self.assertNotIn("password_salt", register_output.getvalue())
            self.assertNotIn("password_hash", login_output.getvalue())
            self.assertNotIn("password_salt", login_output.getvalue())
            login_payload = json.loads(login_output.getvalue())
            token = login_payload["token"]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(database),
                            "--token",
                            token,
                            "list-ledgers",
                        ]
                    ),
                    0,
                )
                self.assertEqual(
                    main(["--db", str(database), "--token", token, "logout"]),
                    0,
                )
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                self.assertEqual(
                    main(
                        [
                            "--db",
                            str(database),
                            "--token",
                            token,
                            "list-ledgers",
                        ]
                    ),
                    1,
                )


if __name__ == "__main__":
    unittest.main()
