"""Authentication, platform administration, and audit services."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from .database import Database
from .errors import (
    AuthenticationError,
    AuthorizationError,
    NotFoundError,
    ValidationError,
)
from .models import AuditEvent, OperationLog, User, UserStatus
from .repositories import LogRepository, UserRepository
from .utils import (
    hash_password,
    json_dumps,
    new_id,
    parse_datetime,
    utc_now,
    utc_now_iso,
    validate_account,
    validate_password,
    verify_password,
)


def _public_user(user: User) -> dict[str, str]:
    """Return only fields that are safe to show in command output."""
    return {
        "user_id": user.user_id,
        "account": user.account,
        "display_name": user.display_name,
        "status": user.status,
    }


def _token_digest(token: str) -> str:
    """Hash a bearer token before it is stored in SQLite."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuditService:
    """Append-only operation log writer and reader."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.logs = LogRepository(db)

    def record(self, event: AuditEvent) -> OperationLog:
        """Append one success or failure event."""
        item = OperationLog(
            log_id=new_id("log"),
            actor_id=event.actor_id,
            ledger_id=event.ledger_id,
            object_type=event.object_type,
            object_id=event.object_id,
            action=event.action,
            before_json=json_dumps(event.before or {}),
            after_json=json_dumps(event.after or {}),
            result=event.result,
            failure_reason=event.failure_reason,
            created_at=utc_now_iso(),
        )
        with self.db.transaction():
            self.logs.add(item)
        return item

    def list_for_ledger(
        self,
        actor_id: str,
        ledger_id: str,
        action: str | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        scope: str = "all",
    ) -> list[OperationLog]:
        """List logs, enforcing the actor's log scope."""
        member = self.db.fetchone(
            "SELECT * FROM ledger_members WHERE ledger_id = ? AND user_id = ? AND status = 'ACTIVE'",
            (ledger_id, actor_id),
        )
        if member is None:
            raise AuthorizationError("当前用户不在账本中")
        is_admin = member["role"] == "LEDGER_ADMIN"
        permissions = set(__import__("json").loads(member["permissions_json"]))
        if scope == "own":
            if not is_admin and "VIEW_OWN_LOGS" not in permissions:
                raise AuthorizationError("没有查看本人日志的权限")
            return [
                item
                for item in self.logs.list(
                    ledger_id, actor_id, action, start_at, end_at
                )
                if item.actor_id == actor_id
            ]
        if not is_admin:
            raise AuthorizationError("只有账本管理员可以查看账本全部日志")
        return self.logs.list(
            ledger_id, actor_id=None, action=action, start_at=start_at, end_at=end_at
        )


class AuthService:
    """Account registration and login."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.users = UserRepository(db)
        self.audit = AuditService(db)

    def register(
        self,
        account: str,
        display_name: str,
        password: str,
        platform_admin: bool = False,
        phone: str = "",
    ) -> User:
        """Register a user and return the persisted object."""
        normalized = validate_account(account)
        validate_password(password)
        if self.users.by_account(normalized):
            raise ValidationError("账号已经存在")
        password_hash, salt = hash_password(password)
        user = User(
            user_id=new_id("usr"),
            account=normalized,
            display_name=display_name.strip() or normalized,
            password_hash=password_hash,
            password_salt=salt,
            status=UserStatus.ACTIVE.value,
            created_at=utc_now_iso(),
            phone=phone.strip(),
        )
        with self.db.transaction():
            self.users.create(user)
            if platform_admin:
                self.db.execute(
                    "INSERT INTO platform_admins(user_id, granted_at) VALUES (?, ?)",
                    (user.user_id, utc_now_iso()),
                )
        return user

    def login(self, account: str, password: str) -> User:
        """Authenticate a user."""
        user = self.users.by_account(account.strip().lower())
        if user is None or not verify_password(
            password, user.password_hash, user.password_salt
        ):
            raise AuthenticationError("账号或密码错误")
        if user.status != UserStatus.ACTIVE.value:
            raise AuthenticationError("账号状态异常，不能登录")
        return user

    def create_session(
        self, account: str, password: str, ttl_hours: int = 12
    ) -> dict[str, Any]:
        """Authenticate a user and issue a revocable bearer token."""
        if ttl_hours <= 0:
            raise ValidationError("登录有效期必须大于 0")
        user = self.login(account, password)
        token = secrets.token_urlsafe(32)
        created_at = utc_now_iso()
        expires_at = (utc_now() + timedelta(hours=ttl_hours)).isoformat()
        with self.db.transaction():
            self.db.execute(
                "INSERT INTO auth_sessions(token_hash, user_id, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (_token_digest(token), user.user_id, created_at, expires_at),
            )
        return {
            "token": token,
            "token_type": "Bearer",
            "expires_at": expires_at,
            "user": _public_user(user),
        }

    def authenticate_session(self, token: str) -> User:
        """Resolve a token to an active user without exposing its hash."""
        if not str(token).strip():
            raise AuthenticationError("缺少登录令牌")
        row = self.db.fetchone(
            "SELECT user_id, expires_at, revoked_at FROM auth_sessions "
            "WHERE token_hash = ?",
            (_token_digest(str(token).strip()),),
        )
        if row is None or row["revoked_at"] is not None:
            raise AuthenticationError("登录状态无效或已经退出")
        try:
            expired = parse_datetime(row["expires_at"]) <= utc_now()
        except ValueError as exc:
            raise AuthenticationError("登录状态异常") from exc
        if expired:
            self.db.execute(
                "UPDATE auth_sessions SET revoked_at = ? "
                "WHERE token_hash = ? AND revoked_at IS NULL",
                (utc_now_iso(), _token_digest(str(token).strip())),
            )
            raise AuthenticationError("登录状态已过期，请重新登录")
        return self.require_active(row["user_id"])

    def revoke_session(self, token: str) -> None:
        """Log out a token immediately."""
        if not str(token).strip():
            raise AuthenticationError("缺少登录令牌")
        result = self.db.execute(
            "UPDATE auth_sessions SET revoked_at = ? "
            "WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now_iso(), _token_digest(str(token).strip())),
        )
        if result.rowcount != 1:
            raise AuthenticationError("登录状态无效或已经退出")

    def require_active(self, user_id: str) -> User:
        """Return an active user or raise a domain error."""
        user = self.users.get(user_id)
        if user is None:
            raise NotFoundError("用户不存在")
        if user.status != UserStatus.ACTIVE.value:
            raise AuthenticationError("账号状态异常")
        return user

    def is_platform_admin(self, user_id: str) -> bool:
        """Check the platform administrator table."""
        return (
            self.db.fetchone(
                "SELECT 1 FROM platform_admins WHERE user_id = ?", (user_id,)
            )
            is not None
        )


class PlatformService:
    """Restricted platform administration for accounts and violations."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.auth = AuthService(db)
        self.audit = AuditService(db)

    def _require_platform_admin(self, actor_id: str) -> None:
        self.auth.require_active(actor_id)
        if not self.auth.is_platform_admin(actor_id):
            raise AuthorizationError("需要平台管理员权限")

    def overview(self, actor_id: str) -> dict[str, int]:
        """Return aggregate counts for the admin dashboard."""
        self._require_platform_admin(actor_id)
        users = self.db.fetchone("SELECT COUNT(*) AS n FROM users")
        ledgers = self.db.fetchone("SELECT COUNT(*) AS n FROM ledgers")
        pending = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM users WHERE status != 'ACTIVE'"
        )
        return {
            "users": int(users["n"]) if users else 0,
            "ledgers": int(ledgers["n"]) if ledgers else 0,
            "non_active_users": int(pending["n"]) if pending else 0,
        }

    def set_account_status(
        self, actor_id: str, target_user_id: str, status: str
    ) -> None:
        """Freeze or activate an account, not a ledger detail."""
        self._require_platform_admin(actor_id)
        if status not in {item.value for item in UserStatus}:
            raise ValidationError("账号状态不合法")
        target = self.auth.users.get(target_user_id)
        if not target:
            raise NotFoundError("目标用户不存在")
        with self.db.transaction():
            self.auth.users.set_status(target_user_id, status)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id="platform",
                    object_type="user",
                    object_id=target_user_id,
                    action="SET_ACCOUNT_STATUS",
                    before={"status": target.status},
                    after={"status": status},
                )
            )

    def list_needed_logs(
        self, actor_id: str, target_user_id: str
    ) -> list[OperationLog]:
        """Return only account/violation-related logs for a platform admin."""
        self._require_platform_admin(actor_id)
        rows = self.db.fetchall(
            "SELECT * FROM operation_logs WHERE ledger_id = 'platform' AND object_id = ? ORDER BY created_at DESC",
            (target_user_id,),
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
