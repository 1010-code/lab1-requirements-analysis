"""Pure helper functions for validation, money, dates, and security."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_RECORD_AMOUNT = Decimal("1000000.00")
MAX_ATTACHMENTS = 3
MAX_ATTACHMENT_SIZE = 5 * 1024 * 1024
ALLOWED_ATTACHMENT_TYPES = {"jpg", "jpeg", "png"}
IDEMPOTENCY_HOURS = 24


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(UTC).replace(microsecond=0)


def utc_now_iso() -> str:
    """Return an ISO-8601 UTC timestamp."""
    return utc_now().isoformat()


def parse_datetime(value: str | datetime) -> datetime:
    """Parse an ISO date or datetime and return an aware datetime."""
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise ValueError("time cannot be empty")
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("time must use ISO format") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def parse_money(value: str | int | Decimal) -> int:
    """Convert a user-entered amount into integer cents."""
    try:
        amount = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("amount must be a decimal number") from exc
    if not amount.is_finite():
        raise ValueError("amount must be finite")
    if amount <= 0:
        raise ValueError("amount must be positive")
    if amount > MAX_RECORD_AMOUNT:
        raise ValueError("amount cannot exceed 1000000.00")
    if amount.as_tuple().exponent < -2:
        raise ValueError("amount supports at most two decimal places")
    return int(amount.quantize(Decimal("0.01")) * 100)


def format_money(cents: int) -> str:
    """Format integer cents as a currency-like decimal string."""
    return f"{Decimal(cents) / Decimal(100):.2f}"


def new_id(prefix: str) -> str:
    """Create a readable unique identifier."""
    return f"{prefix}_{uuid.uuid4().hex}"


def normalize_request_id(request_id: str) -> str:
    """Validate a client idempotency key."""
    value = str(request_id).strip()
    if not value:
        raise ValueError("request id cannot be empty")
    if len(value) > 80:
        raise ValueError("request id cannot exceed 80 characters")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError("request id contains unsupported characters")
    return value


def validate_account(account: str) -> str:
    """Validate a login account and return a normalized value."""
    value = str(account).strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,40}", value):
        raise ValueError("account must be 3-40 letters, numbers, ., _ or -")
    return value


def validate_password(password: str) -> None:
    """Check the minimum password policy used by the prototype."""
    if len(str(password)) < 8:
        raise ValueError("password must contain at least 8 characters")
    if not re.search(r"[A-Za-z]", str(password)):
        raise ValueError("password must contain a letter")
    if not re.search(r"\d", str(password)):
        raise ValueError("password must contain a digit")


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return a salted PBKDF2 password hash and salt hex string."""
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        str(password).encode("utf-8"),
        actual_salt,
        210_000,
    )
    return digest.hex(), actual_salt.hex()


def verify_password(password: str, expected_hash: str, salt_hex: str) -> bool:
    """Compare a password with a stored PBKDF2 hash."""
    try:
        salt = bytes.fromhex(salt_hex)
    except ValueError:
        return False
    actual_hash, _ = hash_password(password, salt)
    return hmac.compare_digest(actual_hash, expected_hash)


def json_dumps(value: Any) -> str:
    """Serialize structured audit/report data consistently."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | bytes | None, default: Any = None) -> Any:
    """Load JSON and return a default for empty values."""
    if value in (None, ""):
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def is_within_hours(iso_text: str, hours: int = IDEMPOTENCY_HOURS) -> bool:
    """Check whether a stored timestamp is still inside the idempotency window."""
    try:
        created = parse_datetime(iso_text)
    except ValueError:
        return False
    return utc_now() - created <= timedelta(hours=hours)


def split_csv_int_list(value: str) -> list[int]:
    """Parse the simple comma-separated test inputs used by the CLI."""
    parts = [part.strip() for part in str(value).split(",") if part.strip()]
    output: list[int] = []
    for part in parts:
        output.append(int(part))
    return output


def parse_member_shares(value: str) -> dict[str, int]:
    """Parse `member_id=amount,member_id=amount` into integer cents."""
    shares: dict[str, int] = {}
    for item in str(value).split(","):
        if not item.strip():
            continue
        if "=" not in item:
            raise ValueError("shares must use member_id=amount pairs")
        member_id, amount_text = item.split("=", 1)
        member_id = member_id.strip()
        if not member_id:
            raise ValueError("member id cannot be empty")
        if member_id in shares:
            raise ValueError("duplicate member in shares")
        shares[member_id] = parse_money(amount_text)
    if not shares:
        raise ValueError("at least one share is required")
    return shares


def validate_attachments(items: Iterable[Any]) -> list[Any]:
    """Validate a list of attachment-like objects."""
    attachments = list(items)
    if len(attachments) > MAX_ATTACHMENTS:
        raise ValueError("each record supports at most 3 attachments")
    for item in attachments:
        name = getattr(item, "file_name", "")
        extension = str(name).rsplit(".", 1)[-1].lower()
        if extension not in ALLOWED_ATTACHMENT_TYPES:
            raise ValueError("attachments must be jpg or png")
        size = int(getattr(item, "file_size", 0))
        if size <= 0 or size > MAX_ATTACHMENT_SIZE:
            raise ValueError("attachment must be between 1 byte and 5MB")
    return attachments


def mask_phone(value: str | None) -> str:
    """Mask a phone number while preserving prefixes and suffixes."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= 7:
        return "*" * len(text)
    return f"{text[:3]}{'*' * (len(text) - 7)}{text[-4:]}"


def safe_percent(numerator: int, denominator: int) -> Decimal:
    """Return a percentage without dividing by zero."""
    if denominator == 0:
        return Decimal(0)
    return (Decimal(numerator) / Decimal(denominator)) * Decimal(100)
