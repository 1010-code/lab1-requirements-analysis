"""Command-line interface for 一同记账."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sqlite3
import sys
from enum import Enum
from typing import Any

from .app import Application
from .demo import run_demo
from .errors import AppError
from .models import UserStatus
from .utils import parse_member_shares, utc_now_iso


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        payload = dataclasses.asdict(value)
        if "password_hash" in payload or "password_salt" in payload:
            return {
                key: _jsonable(payload[key])
                for key in ("user_id", "account", "display_name", "status")
                if key in payload
            }
        return {key: _jsonable(item) for key, item in payload.items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, dict):
        return {
            str(key): _jsonable(item)
            for key, item in value.items()
            if key not in {"password_hash", "password_salt"}
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _print(value: Any) -> None:
    print(json.dumps(_jsonable(value), ensure_ascii=False, indent=2, default=str))


def _attachments(values: list[str] | None) -> list[dict[str, Any]]:
    from .models import Attachment

    result = []
    for raw in values or []:
        if ":" not in raw:
            raise ValueError("attachment must use filename:size format")
        name, size_text = raw.rsplit(":", 1)
        result.append(
            Attachment(
                file_name=name,
                file_type=name.rsplit(".", 1)[-1].lower(),
                file_size=int(size_text),
            )
        )
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yitong", description="一同记账命令行原型")
    parser.add_argument("--db", default="yitong.db", help="SQLite database path")
    parser.add_argument(
        "--token",
        help="登录后返回的令牌；放在子命令之前，例如 --token <TOKEN> list-ledgers",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库")
    demo = sub.add_parser("demo", help="运行完整演示场景")
    demo.add_argument(
        "--persist", action="store_true", help="使用 --db 指定的持久化数据库"
    )
    register = sub.add_parser("register", help="注册用户")
    register.add_argument("--account", required=True)
    register.add_argument("--name", required=True)
    register.add_argument("--password", required=True)
    register.add_argument("--phone", default="")
    register.add_argument("--platform-admin", action="store_true")
    login = sub.add_parser("login", help="登录并获取访问令牌")
    login.add_argument("--account", required=True)
    login.add_argument("--password", required=True)
    sub.add_parser("logout", help="退出当前登录令牌")
    create_ledger = sub.add_parser("create-ledger", help="创建账本")
    create_ledger.add_argument("--owner")
    create_ledger.add_argument("--name", required=True)
    list_ledgers = sub.add_parser("list-ledgers", help="列出用户账本")
    list_ledgers.add_argument("--user")
    invite = sub.add_parser("invite", help="邀请成员")
    invite.add_argument("--admin")
    invite.add_argument("--ledger", required=True)
    invite.add_argument("--account", required=True)
    accept = sub.add_parser("accept-invite", help="接受邀请")
    accept.add_argument("--user")
    accept.add_argument("--invitation", required=True)
    reject = sub.add_parser("reject-invite", help="拒绝邀请")
    reject.add_argument("--user")
    reject.add_argument("--invitation", required=True)
    members = sub.add_parser("members", help="列出账本成员")
    members.add_argument("--user")
    members.add_argument("--ledger", required=True)
    permission = sub.add_parser("set-permission", help="设置成员权限")
    permission.add_argument("--admin")
    permission.add_argument("--ledger", required=True)
    permission.add_argument("--member", required=True)
    permission.add_argument("--permission", required=True)
    permission.add_argument("--off", action="store_true")
    transfer = sub.add_parser("transfer-admin", help="转让管理员")
    transfer.add_argument("--admin")
    transfer.add_argument("--ledger", required=True)
    transfer.add_argument("--member", required=True)
    leave = sub.add_parser("exit-ledger", help="退出账本")
    leave.add_argument("--user")
    leave.add_argument("--ledger", required=True)
    remove = sub.add_parser("remove-member", help="移除成员")
    remove.add_argument("--admin")
    remove.add_argument("--ledger", required=True)
    remove.add_argument("--member", required=True)
    add_record = sub.add_parser("add-record", help="新增收支记录")
    add_record.add_argument("--user")
    add_record.add_argument("--ledger", required=True)
    add_record.add_argument(
        "--type", required=True, choices=["EXPENSE", "INCOME", "expense", "income"]
    )
    add_record.add_argument("--amount", required=True)
    add_record.add_argument("--category", required=True)
    add_record.add_argument("--occurred-at", default=utc_now_iso())
    add_record.add_argument("--note", default="")
    add_record.add_argument("--payer", required=True)
    add_record.add_argument("--shares", required=True, help="member_id=amount,...")
    add_record.add_argument("--request-id", required=True)
    add_record.add_argument("--attachment", action="append")
    add_record.add_argument("--confirm-duplicate", action="store_true")
    list_records = sub.add_parser("list-records", help="查询收支记录")
    list_records.add_argument("--user")
    list_records.add_argument("--ledger", required=True)
    list_records.add_argument("--category")
    list_records.add_argument("--member")
    list_records.add_argument("--status")
    list_records.add_argument("--type", dest="record_type")
    list_records.add_argument("--keyword")
    list_records.add_argument("--amount-min")
    list_records.add_argument("--amount-max")
    get_record = sub.add_parser("get-record", help="查看单条记录")
    get_record.add_argument("--user")
    get_record.add_argument("--record", required=True)
    modify = sub.add_parser("modify-record", help="修改待审核记录")
    modify.add_argument("--user")
    modify.add_argument("--record", required=True)
    modify.add_argument("--amount")
    modify.add_argument("--category")
    modify.add_argument("--note")
    modify.add_argument("--occurred-at")
    modify.add_argument(
        "--shares",
        help="修改金额时必填，格式为 member_id=amount,...",
    )
    modify.add_argument("--version", type=int)
    withdraw = sub.add_parser("withdraw", help="撤回收支记录")
    withdraw.add_argument("--user")
    withdraw.add_argument("--record", required=True)
    withdraw.add_argument("--version", type=int)
    approve = sub.add_parser("approve", help="审核通过")
    approve.add_argument("--user")
    approve.add_argument("--record", required=True)
    approve.add_argument("--version", type=int)
    reject = sub.add_parser("reject", help="驳回记录")
    reject.add_argument("--user")
    reject.add_argument("--record", required=True)
    reject.add_argument("--version", type=int)
    void = sub.add_parser("void", help="作废已审核记录")
    void.add_argument("--admin")
    void.add_argument("--record", required=True)
    void.add_argument("--version", type=int)
    correction = sub.add_parser("add-correction", help="新增差额更正")
    correction.add_argument("--user")
    correction.add_argument("--original", required=True)
    correction.add_argument("--amount", required=True)
    correction.add_argument(
        "--direction",
        required=True,
        choices=["INCREASE", "DECREASE", "increase", "decrease"],
    )
    correction.add_argument("--request-id", required=True)
    correction.add_argument("--note", default="")
    approve_correction = sub.add_parser("approve-correction", help="审核通过更正")
    approve_correction.add_argument("--user")
    approve_correction.add_argument("--correction", required=True)
    reject_correction = sub.add_parser("reject-correction", help="驳回更正")
    reject_correction.add_argument("--user")
    reject_correction.add_argument("--correction", required=True)
    stats = sub.add_parser("statistics", help="查看统计")
    stats.add_argument("--user")
    stats.add_argument("--ledger", required=True)
    stats.add_argument("--category")
    stats.add_argument("--member")
    stats.add_argument("--start")
    stats.add_argument("--end")
    budget = sub.add_parser("set-budget", help="设置预算")
    budget.add_argument("--admin")
    budget.add_argument("--ledger", required=True)
    budget.add_argument("--category", required=True)
    budget.add_argument("--cycle", default="MONTHLY")
    budget.add_argument("--limit", required=True)
    budgets = sub.add_parser("list-budgets", help="查看预算")
    budgets.add_argument("--user")
    budgets.add_argument("--ledger", required=True)
    start_settlement = sub.add_parser("start-settlement", help="发起结算")
    start_settlement.add_argument("--admin")
    start_settlement.add_argument("--ledger", required=True)
    confirm_settlement = sub.add_parser("confirm-settlement", help="确认结算")
    confirm_settlement.add_argument("--user")
    confirm_settlement.add_argument("--settlement", required=True)
    settlement_status = sub.add_parser("settlement-status", help="查看结算状态")
    settlement_status.add_argument("--user")
    settlement_status.add_argument("--settlement", required=True)
    revoke_settlement = sub.add_parser("revoke-settlement", help="撤销结算")
    revoke_settlement.add_argument("--admin")
    revoke_settlement.add_argument("--settlement", required=True)
    logs = sub.add_parser("logs", help="查看操作日志")
    logs.add_argument("--user")
    logs.add_argument("--ledger", required=True)
    logs.add_argument("--scope", default="all", choices=["all", "own"])
    export_stats = sub.add_parser("export-statistics", help="导出统计 CSV")
    export_stats.add_argument("--user")
    export_stats.add_argument("--ledger", required=True)
    export_stats.add_argument("--output", required=True)
    export_settlement = sub.add_parser("export-settlement", help="导出结算名单 CSV")
    export_settlement.add_argument("--admin")
    export_settlement.add_argument("--settlement", required=True)
    export_settlement.add_argument("--output", required=True)
    platform = sub.add_parser("platform-overview", help="平台治理概览")
    platform.add_argument("--admin")
    account_status = sub.add_parser("set-account-status", help="设置账号状态")
    account_status.add_argument("--admin")
    account_status.add_argument("--target", required=True)
    account_status.add_argument(
        "--status", required=True, choices=[item.value for item in UserStatus]
    )
    return parser


def _resolve_actor(app: Application, args: argparse.Namespace, field: str) -> str:
    token = getattr(args, "token", None)
    if token:
        return app.auth.authenticate_session(token).user_id
    value = getattr(args, field, None)
    if not value:
        raise ValueError(f"请提供 --{field}，或先登录并使用 --token")
    return value


def dispatch(app: Application, args: argparse.Namespace) -> Any:
    if args.command == "init":
        return {"database": app.db.path, "initialized": True}
    if args.command == "register":
        return app.auth.register(
            args.account,
            args.name,
            args.password,
            platform_admin=args.platform_admin,
            phone=args.phone,
        )
    if args.command == "login":
        return app.auth.create_session(args.account, args.password)
    if args.command == "logout":
        if not args.token:
            raise ValueError("退出登录需要提供 --token")
        app.auth.revoke_session(args.token)
        return {"logged_out": True}

    actor_fields = [
        field for field in ("admin", "owner", "user") if hasattr(args, field)
    ]
    if len(actor_fields) != 1:
        raise ValueError("命令的身份参数配置不正确")
    actor_field = actor_fields[0]
    setattr(args, actor_field, _resolve_actor(app, args, actor_field))

    if args.command == "create-ledger":
        return app.ledger.create_ledger(args.owner, args.name)
    if args.command == "list-ledgers":
        return app.ledger.list_ledgers(args.user)
    if args.command == "invite":
        return app.ledger.invite(args.admin, args.ledger, args.account)
    if args.command == "accept-invite":
        return app.ledger.accept_invitation(args.user, args.invitation)
    if args.command == "reject-invite":
        app.ledger.reject_invitation(args.user, args.invitation)
        return {"status": "REJECTED"}
    if args.command == "members":
        return app.ledger.list_members(args.user, args.ledger)
    if args.command == "set-permission":
        return app.ledger.set_permission(
            args.admin, args.ledger, args.member, args.permission, not args.off
        )
    if args.command == "transfer-admin":
        app.ledger.transfer_admin(args.admin, args.ledger, args.member)
        return {"status": "TRANSFERRED"}
    if args.command == "exit-ledger":
        app.ledger.exit_ledger(args.user, args.ledger)
        return {"status": "LEFT"}
    if args.command == "remove-member":
        app.ledger.remove_member(args.admin, args.ledger, args.member)
        return {"status": "REMOVED"}
    if args.command == "add-record":
        return app.records.add_record(
            args.user,
            args.ledger,
            args.type,
            args.amount,
            args.category,
            args.occurred_at,
            args.note,
            args.payer,
            parse_member_shares(args.shares),
            args.request_id,
            _attachments(args.attachment),
            args.confirm_duplicate,
        )
    if args.command == "list-records":
        return app.records.query_records(
            args.user,
            args.ledger,
            args.category,
            args.member,
            status=args.status,
            record_type=args.record_type,
            keyword=args.keyword,
            amount_min=args.amount_min,
            amount_max=args.amount_max,
        )
    if args.command == "get-record":
        return app.records.get_record(args.user, args.record)
    if args.command == "modify-record":
        fields = {
            key: value
            for key, value in {
                "amount": args.amount,
                "category": args.category,
                "note": args.note,
                "occurred_at": args.occurred_at,
            }.items()
            if value is not None
        }
        shares = parse_member_shares(args.shares) if args.shares else None
        return app.records.modify_record(
            args.user,
            args.record,
            fields,
            shares=shares,
            expected_version=args.version,
        )
    if args.command == "withdraw":
        return app.records.withdraw_record(args.user, args.record, args.version)
    if args.command == "approve":
        return app.records.approve_record(args.user, args.record, args.version)
    if args.command == "reject":
        return app.records.reject_record(args.user, args.record, args.version)
    if args.command == "void":
        return app.records.void_record(args.admin, args.record, args.version)
    if args.command == "add-correction":
        return app.records.create_correction(
            args.user,
            args.original,
            args.amount,
            args.direction,
            args.request_id,
            args.note,
        )
    if args.command == "approve-correction":
        return app.records.approve_correction(args.user, args.correction)
    if args.command == "reject-correction":
        return app.records.reject_correction(args.user, args.correction)
    if args.command == "statistics":
        return app.records.statistics(
            args.user, args.ledger, args.category, args.member, args.start, args.end
        )
    if args.command == "set-budget":
        return app.records.set_budget(
            args.admin, args.ledger, args.category, args.cycle, args.limit
        )
    if args.command == "list-budgets":
        return app.records.list_budgets(args.user, args.ledger)
    if args.command == "start-settlement":
        return app.settlements.start(args.admin, args.ledger)
    if args.command == "confirm-settlement":
        return app.settlements.confirm(args.user, args.settlement)
    if args.command == "settlement-status":
        return app.settlements.status(args.user, args.settlement)
    if args.command == "revoke-settlement":
        return app.settlements.revoke(args.admin, args.settlement)
    if args.command == "logs":
        return app.audit.list_for_ledger(args.user, args.ledger, scope=args.scope)
    if args.command == "export-statistics":
        return app.reporting.export_statistics_csv(args.user, args.ledger, args.output)
    if args.command == "export-settlement":
        return app.reporting.export_settlement_csv(
            args.admin, args.settlement, args.output
        )
    if args.command == "platform-overview":
        return app.platform.overview(args.admin)
    if args.command == "set-account-status":
        app.platform.set_account_status(args.admin, args.target, args.status)
        return {"status": args.status}
    raise ValueError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "demo":
            path = args.db if args.persist else ":memory:"
            _print(run_demo(path))
            return 0
        app = Application.create(args.db)
        try:
            _print(dispatch(app, args))
        finally:
            app.close()
        return 0
    except (AppError, ValueError, sqlite3.IntegrityError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
