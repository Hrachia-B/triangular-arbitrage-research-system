"""Append-only research artifacts for live paper simulations.

JSONL writes are serialized with an ``asyncio.Lock`` and each line is flushed
before returning.  A process crash can therefore lose at most the line currently
being written; completed lines remain independently parseable.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]+")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "to_record") and callable(value.to_record):
        return value.to_record()
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"cannot encode {type(value).__name__} as JSON")


def _record_value(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if hasattr(value, "to_record") and callable(value.to_record):
        return value.to_record()
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    return value


class JSONLRecorder:
    """Run-scoped JSONL recorder with small-file rotation.

    The constructor creates only local data directories.  ``start`` is provided
    for symmetric async lifecycle code but is optional.
    """

    _CATEGORY_LOCATIONS: ClassVar[dict[str, tuple[str, str]]] = {
        "health": ("raw", "order_book_health"),
        "book_health": ("raw", "order_book_health"),
        "raw_opportunity": ("raw", "raw_opportunities"),
        "after_fees": ("signals", "after_fees"),
        "depth": ("signals", "depth_opportunities"),
        "depth_opportunity": ("signals", "depth_opportunities"),
        "signal": ("signals", "signals"),
        "opportunity": ("signals", "signals"),
        "latency": ("signals", "latency_rechecks"),
        "pessimistic": ("signals", "pessimistic"),
        "snapshot": ("snapshots", "book_snapshots"),
        "summary": ("reports", "summary_metrics"),
        "error": ("logs", "errors"),
        "log": ("logs", "events"),
    }

    def __init__(
        self,
        output_dir: str | Path = "data",
        *,
        run_id: str | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        fsync: bool = False,
        record_observer: Callable[[str, Any], None] | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.output_dir = Path(output_dir)
        timestamp = _utc_now().strftime("%Y%m%dT%H%M%SZ")
        safe_run_id = _SAFE_NAME.sub("_", run_id or f"{timestamp}_{uuid.uuid4().hex[:8]}")
        self.run_id = safe_run_id.strip("._") or timestamp
        self.max_bytes = int(max_bytes)
        self.fsync = fsync
        self._record_observer = record_observer
        self.raw_dir = self.output_dir / "raw"
        self.signals_dir = self.output_dir / "signals"
        self.reports_dir = self.output_dir / "reports"
        self.snapshots_dir = self.output_dir / "snapshots"
        self.logs_dir = self.output_dir / "logs"
        for directory in (
            self.output_dir,
            self.raw_dir,
            self.signals_dir,
            self.reports_dir,
            self.snapshots_dir,
            self.logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._closed = False
        self._paths: dict[str, Path] = {}

    async def start(self) -> JSONLRecorder:
        if self._closed:
            raise RuntimeError("recorder is closed")
        return self

    def _safe_category(self, category: str) -> str:
        name = _SAFE_NAME.sub("_", category.strip().lower()).strip("._")
        if not name:
            raise ValueError("category cannot be empty")
        return name

    def path_for(self, category: str) -> Path:
        safe = self._safe_category(category)
        if safe not in self._paths:
            folder_name, stem = self._CATEGORY_LOCATIONS.get(safe, ("raw", safe))
            folder = getattr(self, f"{folder_name}_dir")
            self._paths[safe] = folder / f"{stem}_{self.run_id}.jsonl"
        return self._paths[safe]

    @property
    def paths(self) -> Mapping[str, Path]:
        return dict(self._paths)

    def _rotate_if_needed(self, path: Path, incoming_bytes: int) -> None:
        try:
            current_size = path.stat().st_size
        except FileNotFoundError:
            return
        if current_size + incoming_bytes <= self.max_bytes:
            return
        number = 1
        while Path(f"{path}.{number}").exists():
            number += 1
        path.replace(Path(f"{path}.{number}"))

    def _write_envelope(self, path: Path, envelope: Mapping[str, Any]) -> None:
        """Serialize and append off the event-loop thread."""

        line = (
            json.dumps(
                envelope,
                default=_json_default,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
            + "\n"
        )
        encoded_size = len(line.encode("utf-8"))
        self._rotate_if_needed(path, encoded_size)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())

    async def record(self, category: str, record: Any, **context: Any) -> Path:
        """Append one self-describing line and return its artifact path."""

        if self._closed:
            raise RuntimeError("recorder is closed")
        safe = self._safe_category(category)
        path = self.path_for(safe)
        envelope = {
            "recorded_at": _utc_now().isoformat(),
            "run_id": self.run_id,
            "category": safe,
            "data": _record_value(record),
        }
        if context:
            envelope["context"] = context
        async with self._lock:
            # JSON serialization, open/write/flush, rotation, and optional fsync
            # can all block.  Offloading preserves WebSocket consumption and
            # latency timers while the scanner applies natural backpressure.
            await asyncio.to_thread(self._write_envelope, path, envelope)
            if self._record_observer is not None:
                # The observer is intentionally synchronous and lightweight.
                # It runs only after the JSONL append succeeds, so cumulative
                # in-memory report state never gets ahead of durable records.
                self._record_observer(safe, envelope["data"])
        return path

    async def record_many(
        self, category: str, records: Iterable[Any], **context: Any
    ) -> Path | None:
        path: Path | None = None
        for record in records:
            path = await self.record(category, record, **context)
        return path

    async def record_signal(self, record: Any, *, stage: str = "signal", **context: Any) -> Path:
        return await self.record(stage, record, **context)

    async def record_latency(self, record: Any, **context: Any) -> Path:
        return await self.record("latency", record, **context)

    async def record_health(self, record: Any, **context: Any) -> Path:
        return await self.record("health", record, **context)

    async def record_pessimistic(self, record: Any, **context: Any) -> Path:
        return await self.record("pessimistic", record, **context)

    async def record_summary(self, record: Any, **context: Any) -> Path:
        return await self.record("summary", record, **context)

    def write_selection(self, name: str, records: Iterable[Any], **metadata: Any) -> Path:
        """Write a compact discovery artifact before streaming begins."""

        if self._closed:
            raise RuntimeError("recorder is closed")
        safe = self._safe_category(name)
        if not safe.startswith("selected_"):
            safe = f"selected_{safe}"
        path = self.raw_dir / f"{safe}_{self.run_id}.json"
        payload = {
            "recorded_at": _utc_now().isoformat(),
            "run_id": self.run_id,
            "kind": safe,
            "metadata": metadata,
            "records": [_record_value(record) for record in records],
        }
        rendered = (
            json.dumps(
                payload, default=_json_default, indent=2, ensure_ascii=False, allow_nan=False
            )
            + "\n"
        )
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(rendered, encoding="utf-8")
        temporary.replace(path)
        return path

    def close(self) -> None:
        """Prevent future writes (there are no buffered file handles to drain)."""

        self._closed = True

    async def aclose(self) -> None:
        async with self._lock:
            self.close()

    async def __aenter__(self) -> JSONLRecorder:
        return await self.start()

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        await self.aclose()


Recorder = JSONLRecorder


__all__ = ["JSONLRecorder", "Recorder"]
