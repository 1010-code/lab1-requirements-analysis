"""Ledger, membership, invitation, and permission services."""

from __future__ import annotations

import json

from .auth_service import AuditService, AuthService
from .database import Database
from .errors import AuthorizationError, ConflictError, NotFoundError, ValidationError
from .models import (
    DEFAULT_MEMBER_PERMISSIONS,
    AuditEvent,
    Invitation,
    InvitationStatus,
    Ledger,
    LedgerMember,
    LedgerRole,
    Permission,
)
from .repositories import (
    InvitationRepository,
    LedgerRepository,
    MemberRepository,
    UserRepository,
)
from .utils import new_id, utc_now_iso


class LedgerService:
    """Handles ledger membership and the permission matrix."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.auth = AuthService(db)
        self.audit = AuditService(db)
        self.users = UserRepository(db)
        self.ledgers = LedgerRepository(db)
        self.members = MemberRepository(db)
        self.invitations = InvitationRepository(db)

    def _require_active_member(self, user_id: str, ledger_id: str) -> LedgerMember:
        self.auth.require_active(user_id)
        member = self.members.by_user(ledger_id, user_id)
        if member is None or member.status != "ACTIVE":
            raise AuthorizationError("当前用户不是该账本的有效成员")
        return member

    def _require_admin(self, user_id: str, ledger_id: str) -> LedgerMember:
        member = self._require_active_member(user_id, ledger_id)
        if member.role != LedgerRole.LEDGER_ADMIN.value:
            raise AuthorizationError("只有账本管理员可以执行此操作")
        return member

    def create_ledger(self, owner_id: str, name: str) -> Ledger:
        """Create a ledger and make its owner the first administrator."""
        self.auth.require_active(owner_id)
        ledger_name = name.strip()
        if not ledger_name:
            raise ValidationError("账本名称不能为空")
        ledger = Ledger(
            ledger_id=new_id("led"),
            name=ledger_name,
            owner_id=owner_id,
            status="ACTIVE",
            created_at=utc_now_iso(),
        )
        owner = LedgerMember(
            member_id=new_id("mem"),
            ledger_id=ledger.ledger_id,
            user_id=owner_id,
            role=LedgerRole.LEDGER_ADMIN.value,
            permissions={p.value for p in Permission},
            status="ACTIVE",
            permission_version=1,
            joined_at=utc_now_iso(),
        )
        with self.db.transaction():
            self.ledgers.create(ledger)
            self.members.create(owner)
            self.audit.record(
                AuditEvent(
                    actor_id=owner_id,
                    ledger_id=ledger.ledger_id,
                    object_type="ledger",
                    object_id=ledger.ledger_id,
                    action="CREATE_LEDGER",
                    after={"name": ledger_name},
                )
            )
        return ledger

    def list_ledgers(self, user_id: str) -> list[Ledger]:
        """List active ledgers for a user."""
        self.auth.require_active(user_id)
        return self.ledgers.list_for_user(user_id)

    def get_ledger(self, user_id: str, ledger_id: str) -> Ledger:
        """Load a ledger after checking active membership."""
        self._require_active_member(user_id, ledger_id)
        ledger = self.ledgers.get(ledger_id)
        if ledger is None:
            raise NotFoundError("账本不存在")
        return ledger

    def list_members(self, user_id: str, ledger_id: str) -> list[LedgerMember]:
        """List members after checking ledger access."""
        self._require_active_member(user_id, ledger_id)
        return self.members.list_for_ledger(ledger_id)

    def invite(self, actor_id: str, ledger_id: str, invitee_account: str) -> Invitation:
        """Invite a user; membership starts only after acceptance."""
        self._require_admin(actor_id, ledger_id)
        invitee = self.users.by_account(invitee_account.strip().lower())
        if invitee is None:
            raise NotFoundError("被邀请用户不存在")
        if self.members.by_user(ledger_id, invitee.user_id):
            raise ConflictError("该用户已经是账本成员")
        if self.invitations.pending_between(ledger_id, invitee.user_id):
            raise ConflictError("该用户已有待接受邀请")
        invitation = Invitation(
            invitation_id=new_id("inv"),
            ledger_id=ledger_id,
            inviter_id=actor_id,
            invitee_id=invitee.user_id,
            status=InvitationStatus.PENDING.value,
            created_at=utc_now_iso(),
        )
        with self.db.transaction():
            self.invitations.create(invitation)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="invitation",
                    object_id=invitation.invitation_id,
                    action="INVITE_MEMBER",
                    after={"invitee_id": invitee.user_id},
                )
            )
        return invitation

    def accept_invitation(self, user_id: str, invitation_id: str) -> LedgerMember:
        """Accept an invitation and create a default member."""
        self.auth.require_active(user_id)
        invitation = self.invitations.get(invitation_id)
        if invitation is None:
            raise NotFoundError("邀请不存在")
        if invitation.invitee_id != user_id:
            raise AuthorizationError("该邀请不属于当前用户")
        if invitation.status != InvitationStatus.PENDING.value:
            raise ConflictError("邀请已处理或已失效")
        existing = self.members.by_user(invitation.ledger_id, user_id)
        if existing and existing.status == "ACTIVE":
            raise ConflictError("已经是该账本成员")
        with self.db.transaction():
            if existing:
                self.db.execute(
                    "UPDATE ledger_members SET status = 'ACTIVE', left_at = NULL, role = 'MEMBER', permissions_json = ?, permission_version = permission_version + 1 WHERE member_id = ?",
                    (
                        json.dumps(sorted(DEFAULT_MEMBER_PERMISSIONS)),
                        existing.member_id,
                    ),
                )
                member = self.members.get(existing.member_id)
            else:
                member = LedgerMember(
                    member_id=new_id("mem"),
                    ledger_id=invitation.ledger_id,
                    user_id=user_id,
                    role=LedgerRole.MEMBER.value,
                    permissions=set(DEFAULT_MEMBER_PERMISSIONS),
                    status="ACTIVE",
                    permission_version=1,
                    joined_at=utc_now_iso(),
                )
                self.members.create(member)
            self.invitations.set_status(
                invitation_id, InvitationStatus.ACCEPTED.value, utc_now_iso()
            )
            self.audit.record(
                AuditEvent(
                    actor_id=user_id,
                    ledger_id=invitation.ledger_id,
                    object_type="invitation",
                    object_id=invitation_id,
                    action="ACCEPT_INVITATION",
                    after={"member_id": member.member_id if member else None},
                )
            )
        if member is None:
            raise ConflictError("加入账本失败")
        return member

    def reject_invitation(self, user_id: str, invitation_id: str) -> None:
        """Reject an invitation without creating a membership."""
        invitation = self.invitations.get(invitation_id)
        if invitation is None or invitation.invitee_id != user_id:
            raise NotFoundError("邀请不存在")
        if invitation.status != InvitationStatus.PENDING.value:
            raise ConflictError("邀请已处理")
        with self.db.transaction():
            self.invitations.set_status(
                invitation_id, InvitationStatus.REJECTED.value, utc_now_iso()
            )
            self.audit.record(
                AuditEvent(
                    actor_id=user_id,
                    ledger_id=invitation.ledger_id,
                    object_type="invitation",
                    object_id=invitation_id,
                    action="REJECT_INVITATION",
                )
            )

    def set_permission(
        self,
        actor_id: str,
        ledger_id: str,
        target_member_id: str,
        permission: str,
        enabled: bool,
    ) -> LedgerMember:
        """Grant or revoke one member permission."""
        self._require_admin(actor_id, ledger_id)
        if permission not in {item.value for item in Permission}:
            raise ValidationError("权限名称不合法")
        target = self.members.get(target_member_id)
        if target is None or target.ledger_id != ledger_id or target.status != "ACTIVE":
            raise NotFoundError("目标成员不存在")
        updated = set(target.permissions)
        if enabled:
            updated.add(permission)
        else:
            updated.discard(permission)
        with self.db.transaction():
            self.members.update_permissions(target_member_id, updated)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="member",
                    object_id=target_member_id,
                    action="SET_PERMISSION",
                    before={"permission": permission, "enabled": not enabled},
                    after={"permission": permission, "enabled": enabled},
                )
            )
        refreshed = self.members.get(target_member_id)
        if refreshed is None:
            raise NotFoundError("成员不存在")
        return refreshed

    def transfer_admin(
        self, actor_id: str, ledger_id: str, target_member_id: str
    ) -> None:
        """Transfer ledger ownership to an active member."""
        actor = self._require_admin(actor_id, ledger_id)
        target = self.members.get(target_member_id)
        if target is None or target.ledger_id != ledger_id or target.status != "ACTIVE":
            raise NotFoundError("目标成员不存在")
        if target.member_id == actor.member_id:
            raise ConflictError("不能把管理员转给自己")
        with self.db.transaction():
            self.members.update_role(target.member_id, LedgerRole.LEDGER_ADMIN.value)
            self.members.update_role(actor.member_id, LedgerRole.MEMBER.value)
            self.ledgers.update_owner(ledger_id, target.user_id)
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="ledger",
                    object_id=ledger_id,
                    action="TRANSFER_ADMIN",
                    before={"owner_id": actor.user_id},
                    after={"owner_id": target.user_id},
                )
            )

    def _pending_counts(self, ledger_id: str, member: LedgerMember) -> tuple[int, int]:
        pending_records = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM expense_records WHERE ledger_id = ? AND submitter_id = ? AND status = 'PENDING'",
            (ledger_id, member.user_id),
        )
        pending_confirmations = self.db.fetchone(
            "SELECT COUNT(*) AS n FROM settlement_confirmations c JOIN settlements s ON s.settlement_id = c.settlement_id WHERE s.ledger_id = ? AND s.status = 'PENDING' AND c.member_id = ? AND c.status = 'PENDING'",
            (ledger_id, member.member_id),
        )
        return (
            int(pending_records["n"]) if pending_records else 0,
            int(pending_confirmations["n"]) if pending_confirmations else 0,
        )

    def exit_ledger(self, actor_id: str, ledger_id: str) -> None:
        """Exit when no pending record or pending settlement confirmation remains."""
        member = self._require_active_member(actor_id, ledger_id)
        if (
            member.role == LedgerRole.LEDGER_ADMIN.value
            and self.members.count_active_admins(ledger_id) <= 1
        ):
            raise ConflictError("唯一管理员不能直接退出，请先转让管理员")
        pending_records, pending_confirmations = self._pending_counts(ledger_id, member)
        if pending_records or pending_confirmations:
            raise ConflictError("存在未处理记录或未确认结算，不能退出")
        with self.db.transaction():
            self.members.deactivate(member.member_id, utc_now_iso())
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="member",
                    object_id=member.member_id,
                    action="EXIT_LEDGER",
                )
            )

    def remove_member(
        self, actor_id: str, ledger_id: str, target_member_id: str
    ) -> None:
        """Remove a member after checking pending work and administrator count."""
        self._require_admin(actor_id, ledger_id)
        target = self.members.get(target_member_id)
        if target is None or target.ledger_id != ledger_id or target.status != "ACTIVE":
            raise NotFoundError("目标成员不存在")
        if (
            target.role == LedgerRole.LEDGER_ADMIN.value
            and self.members.count_active_admins(ledger_id) <= 1
        ):
            raise ConflictError("不能移除唯一管理员")
        pending_records, pending_confirmations = self._pending_counts(ledger_id, target)
        if pending_records or pending_confirmations:
            raise ConflictError("目标成员仍有未处理记录或未确认结算")
        with self.db.transaction():
            self.members.deactivate(target_member_id, utc_now_iso())
            self.audit.record(
                AuditEvent(
                    actor_id=actor_id,
                    ledger_id=ledger_id,
                    object_type="member",
                    object_id=target_member_id,
                    action="REMOVE_MEMBER",
                )
            )

    def member_permissions(self, user_id: str, ledger_id: str) -> set[str]:
        """Return effective permissions for a user in a ledger."""
        member = self._require_active_member(user_id, ledger_id)
        if member.role == LedgerRole.LEDGER_ADMIN.value:
            return {item.value for item in Permission}
        return set(member.permissions)

    def require_permission(
        self, user_id: str, ledger_id: str, permission: str
    ) -> LedgerMember:
        """Check one permission and return the member."""
        member = self._require_active_member(user_id, ledger_id)
        effective = (
            {item.value for item in Permission}
            if member.role == LedgerRole.LEDGER_ADMIN.value
            else set(member.permissions)
        )
        if permission not in effective:
            raise AuthorizationError("没有执行该操作所需的权限")
        return member
