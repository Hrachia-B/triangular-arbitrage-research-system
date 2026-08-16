"""Append-only research artifacts for live paper simulations.

JSONL writes are serialized with an ``asyncio.Lock`` and each line is flushed
before returning.  A process crash can therefore lose at most the line currently
being written; completed lines remain independently parseable.
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
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
        storage_mode: str = "full",
        raw_sample_rate: float = 0.001,
        top_n: int = 1_000,
        near_break_even_threshold: Decimal = Decimal("-0.0005"),
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
        self.storage_mode = storage_mode.strip().lower()
        if self.storage_mode not in {"full", "compact"}:
            raise ValueError("storage_mode must be 'full' or 'compact'")
        self.raw_sample_rate = float(raw_sample_rate)
        if not 0 <= self.raw_sample_rate <= 1:
            raise ValueError("raw_sample_rate must be in [0, 1]")
        self.top_n = int(top_n)
        if self.top_n < 1:
            raise ValueError("top_n must be positive")
        self.near_break_even_threshold = Decimal(str(near_break_even_threshold))
        self._retained_signal_ids: set[str] = set()
        self._top_sequence = 0
        self._top_records: dict[str, list[tuple[Decimal, int, Any]]] = {
            "raw": [],
            "net": [],
            "realistic": [],
        }
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

    @staticmethod
    def _decimal_field(record: Mapping[str, Any], *names: str) -> Decimal | None:
        for name in names:
            value = record.get(name)
            if value is None or isinstance(value, bool):
                continue
            try:
                return Decimal(str(value))
            except Exception:
                continue
        return None

    def _sampled(self, category: str, record: Mapping[str, Any]) -> bool:
        if self.raw_sample_rate <= 0:
            return False
        if self.raw_sample_rate >= 1:
            return True
        identity = record.get("signal_id") or record.get("opportunity_id")
        if not identity:
            identity = json.dumps(record, default=_json_default, sort_keys=True)
        digest = hashlib.blake2b(
            f"{self.run_id}:{category}:{identity}".encode(), digest_size=8
        ).digest()
        ratio = int.from_bytes(digest, "big") / ((1 << 64) - 1)
        return ratio < self.raw_sample_rate

    def _consider_top(self, name: str, score: Decimal | None, record: Any) -> None:
        if score is None:
            return
        self._top_sequence += 1
        item = (score, self._top_sequence, record)
        heap = self._top_records[name]
        if len(heap) < self.top_n:
            heapq.heappush(heap, item)
        elif score > heap[0][0]:
            heapq.heapreplace(heap, item)

    def _compact_should_persist(self, category: str, record: Mapping[str, Any]) -> bool:
        signal_id = str(record.get("signal_id") or record.get("opportunity_id") or "")
        if category == "raw_opportunity":
            self._consider_top("raw", self._decimal_field(record, "raw_return"), dict(record))
            return self._sampled(category, record)
        if category in {"signal", "opportunity"}:
            net = self._decimal_field(record, "return_after_fees", "net_return")
            realistic = self._decimal_field(
                record, "pessimistic_return", "return_after_depth", "realistic_return"
            )
            self._consider_top("net", net, dict(record))
            self._consider_top("realistic", realistic, dict(record))
            profitable = any(
                bool(record.get(field))
                for field in (
                    "profitable_after_fees",
                    "profitable_after_depth",
                    "profitable_pessimistic",
                )
            )
            keep = (
                profitable
                or (net is not None and net > self.near_break_even_threshold)
                or self._sampled(category, record)
            )
            if keep and signal_id:
                self._retained_signal_ids.add(signal_id)
            return keep
        if category in {"after_fees", "depth"}:
            if signal_id:
                self._retained_signal_ids.add(signal_id)
            return True
        if category == "pessimistic":
            keep = bool(record.get("profitable_pessimistic"))
            if keep and signal_id:
                self._retained_signal_ids.add(signal_id)
            return keep
        if category == "latency":
            return bool(signal_id and signal_id in self._retained_signal_ids)
        return True

    def _write_compact_top_records(self) -> None:
        if self.storage_mode != "compact":
            return
        payload = {
            "recorded_at": _utc_now().isoformat(),
            "run_id": self.run_id,
            "storage_mode": self.storage_mode,
            "top_n": self.top_n,
            "top_raw_opportunities": [
                record for _, _, record in sorted(self._top_records["raw"], reverse=True)
            ],
            "top_net_opportunities": [
                record for _, _, record in sorted(self._top_records["net"], reverse=True)
            ],
            "top_realistic_opportunities": [
                record for _, _, record in sorted(self._top_records["realistic"], reverse=True)
            ],
        }
        path = self.signals_dir / f"compact_top_opportunities_{self.run_id}.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, default=_json_default, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(path)

    async def record(self, category: str, record: Any, **context: Any) -> Path:
        """Append one self-describing line and return its artifact path."""

        if self._closed:
            raise RuntimeError("recorder is closed")
        safe = self._safe_category(category)
        path = self.path_for(safe)
        value = _record_value(record)
        envelope = {
            "recorded_at": _utc_now().isoformat(),
            "run_id": self.run_id,
            "category": safe,
            "data": value,
        }
        if context:
            envelope["context"] = context
        persist = True
        if self.storage_mode == "compact" and isinstance(value, Mapping):
            persist = self._compact_should_persist(safe, value)
        async with self._lock:
            # JSON serialization, open/write/flush, rotation, and optional fsync
            # can all block.  Offloading preserves WebSocket consumption and
            # latency timers while the scanner applies natural backpressure.
            if persist:
                await asyncio.to_thread(self._write_envelope, path, envelope)
            if self._record_observer is not None:
                # In full mode this runs only after a successful durable append.
                # Compact mode also observes deliberately discarded samples so
                # cumulative reports remain exact without retaining every payload.
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

        if not self._closed:
            self._write_compact_top_records()
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
