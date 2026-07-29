"""Small shared helpers with no exchange or trading side effects."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    """Return an aware UTC wall-clock timestamp."""

    return datetime.now(UTC)


def utc_timestamp() -> str:
    """Return a filesystem-safe UTC run timestamp."""

    return utc_now().strftime("%Y%m%dT%H%M%S_%fZ")


def iso_utc(value: datetime | None = None) -> str:
    """Serialize a datetime in an unambiguous ISO-8601 UTC form."""

    current = value or utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    """Recursively convert common research-domain objects to JSON values."""

    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return iso_utc(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value
