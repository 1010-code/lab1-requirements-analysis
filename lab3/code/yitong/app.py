"""Application composition root."""

from __future__ import annotations

from .auth_service import AuditService, AuthService, PlatformService
from .database import Database
from .ledger_service import LedgerService
from .record_service import RecordService
from .reporting_service import ReportingService
from .settlement_service import SettlementService


class Application:
    """Small service container used by the CLI and tests."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.db.initialize()
        self.auth = AuthService(db)
        self.audit = AuditService(db)
        self.ledger = LedgerService(db)
        self.records = RecordService(db)
        self.settlements = SettlementService(db)
        self.reporting = ReportingService(db)
        self.platform = PlatformService(db)

    @classmethod
    def create(cls, path: str = "yitong.db") -> Application:
        """Create and initialize an application."""
        return cls(Database(path))

    def close(self) -> None:
        """Close the persistent connection."""
        self.db.close()
