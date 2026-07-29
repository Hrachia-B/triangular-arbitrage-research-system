"""Protocol Buffers codec for MEXC Spot V3 aggregate-depth frames.

The compact descriptor below mirrors the public MEXC schemas at commit
``7b8ac7a6681f28551612a5a7cefbb7e09b56bb85``.  It intentionally declares only
the aggregate-depth branch of ``PushDataV3ApiWrapper``; protobuf safely preserves
wire compatibility while ignoring all unrelated oneof branches.

Official schemas:
https://github.com/mexcdevelop/websocket-proto/blob/main/PushDataV3ApiWrapper.proto
https://github.com/mexcdevelop/websocket-proto/blob/main/PublicAggreDepthsV3Api.proto
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

MEXC_PROTO_SCHEMA_COMMIT = "7b8ac7a6681f28551612a5a7cefbb7e09b56bb85"


class MexcProtobufError(ValueError):
    """Raised when a binary MEXC frame is malformed or is not aggregate depth."""


@dataclass(frozen=True, slots=True)
class MexcProtoDepthUpdate:
    """String-preserving representation decoded from the official protobuf frame."""

    channel: str
    symbol: str
    from_version: str
    to_version: str
    bids: tuple[tuple[str, str], ...]
    asks: tuple[tuple[str, str], ...]
    event_type: str = ""
    create_time_ms: int | None = None
    send_time_ms: int | None = None
    last_order_create_time_ms: int | None = None


@lru_cache(maxsize=1)
def _message_classes() -> tuple[type[Any], type[Any], type[Any]]:
    """Build protobuf classes from the exact public wire-field definitions."""

    try:
        from google.protobuf import descriptor_pb2, descriptor_pool, message_factory
    except ImportError as exc:  # pragma: no cover - depends on installation profile.
        raise RuntimeError("MEXC protobuf decoding requires the 'protobuf' package") from exc

    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "tri_arb_mexc_depth.proto"
    file_proto.package = "tri_arb.mexc"
    file_proto.syntax = "proto3"

    item = file_proto.message_type.add()
    item.name = "PublicAggreDepthV3ApiItem"
    _add_field(
        item,
        name="price",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        item,
        name="quantity",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )

    depth = file_proto.message_type.add()
    depth.name = "PublicAggreDepthsV3Api"
    _add_field(
        depth,
        name="asks",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type_name=".tri_arb.mexc.PublicAggreDepthV3ApiItem",
    )
    _add_field(
        depth,
        name="bids",
        number=2,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        label=descriptor_pb2.FieldDescriptorProto.LABEL_REPEATED,
        type_name=".tri_arb.mexc.PublicAggreDepthV3ApiItem",
    )
    _add_field(
        depth,
        name="eventType",
        number=3,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        depth,
        name="fromVersion",
        number=4,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        depth,
        name="toVersion",
        number=5,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        depth,
        name="lastOrderCreateTime",
        number=6,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    )

    wrapper = file_proto.message_type.add()
    wrapper.name = "PushDataV3ApiWrapper"
    _add_field(
        wrapper,
        name="channel",
        number=1,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    wrapper.oneof_decl.add().name = "body"
    _add_field(
        wrapper,
        name="publicAggreDepths",
        number=313,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_MESSAGE,
        type_name=".tri_arb.mexc.PublicAggreDepthsV3Api",
        oneof_index=0,
    )
    _add_field(
        wrapper,
        name="symbol",
        number=3,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        wrapper,
        name="symbolId",
        number=4,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_STRING,
    )
    _add_field(
        wrapper,
        name="createTime",
        number=5,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    )
    _add_field(
        wrapper,
        name="sendTime",
        number=6,
        field_type=descriptor_pb2.FieldDescriptorProto.TYPE_INT64,
    )

    pool = descriptor_pool.DescriptorPool()
    pool.AddSerializedFile(file_proto.SerializeToString())
    item_descriptor = pool.FindMessageTypeByName("tri_arb.mexc.PublicAggreDepthV3ApiItem")
    depth_descriptor = pool.FindMessageTypeByName("tri_arb.mexc.PublicAggreDepthsV3Api")
    wrapper_descriptor = pool.FindMessageTypeByName("tri_arb.mexc.PushDataV3ApiWrapper")
    return (
        message_factory.GetMessageClass(item_descriptor),
        message_factory.GetMessageClass(depth_descriptor),
        message_factory.GetMessageClass(wrapper_descriptor),
    )


def _add_field(
    message: Any,
    *,
    name: str,
    number: int,
    field_type: int,
    label: int | None = None,
    type_name: str | None = None,
    oneof_index: int | None = None,
) -> None:
    field = message.field.add()
    field.name = name
    field.number = number
    field.type = field_type
    if label is not None:
        field.label = label
    if type_name is not None:
        field.type_name = type_name
    if oneof_index is not None:
        field.oneof_index = oneof_index


def protobuf_message_classes() -> tuple[type[Any], type[Any], type[Any]]:
    """Expose the generated runtime classes for fixture construction/introspection."""

    return _message_classes()


def decode_mexc_depth_frame(payload: bytes | bytearray | memoryview) -> MexcProtoDepthUpdate:
    """Deserialize one binary ``PushDataV3ApiWrapper`` aggregate-depth frame."""

    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("MEXC protobuf payload must be bytes-like")
    raw = bytes(payload)
    if not raw:
        raise MexcProtobufError("MEXC protobuf payload is empty")

    _, _, wrapper_class = _message_classes()
    wrapper = wrapper_class()
    try:
        wrapper.ParseFromString(raw)
    except Exception as exc:
        raise MexcProtobufError("invalid MEXC protobuf frame") from exc

    if not wrapper.HasField("publicAggreDepths"):
        raise MexcProtobufError("MEXC protobuf frame is not an aggregate-depth update")
    depth = wrapper.publicAggreDepths
    if not wrapper.symbol:
        raise MexcProtobufError("MEXC aggregate-depth frame has no symbol")
    if not depth.fromVersion or not depth.toVersion:
        raise MexcProtobufError("MEXC aggregate-depth frame has no version range")

    return MexcProtoDepthUpdate(
        channel=wrapper.channel,
        symbol=wrapper.symbol,
        from_version=depth.fromVersion,
        to_version=depth.toVersion,
        bids=tuple((level.price, level.quantity) for level in depth.bids),
        asks=tuple((level.price, level.quantity) for level in depth.asks),
        event_type=depth.eventType,
        create_time_ms=wrapper.createTime or None,
        send_time_ms=wrapper.sendTime or None,
        last_order_create_time_ms=depth.lastOrderCreateTime or None,
    )


def encode_mexc_depth_frame(
    *,
    symbol: str,
    from_version: str | int,
    to_version: str | int,
    bids: tuple[tuple[str, str], ...] = (),
    asks: tuple[tuple[str, str], ...] = (),
    interval_ms: int = 100,
    send_time_ms: int | None = None,
    create_time_ms: int | None = None,
    last_order_create_time_ms: int | None = None,
) -> bytes:
    """Build an official-wire-compatible frame for deterministic replay tests."""

    if interval_ms not in {10, 100}:
        raise ValueError("MEXC aggregate-depth interval must be 10 or 100 ms")
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol or not normalized_symbol.isalnum():
        raise ValueError("MEXC symbol must be non-empty and alphanumeric")

    _, _, wrapper_class = _message_classes()
    wrapper = wrapper_class()
    wrapper.channel = f"spot@public.aggre.depth.v3.api.pb@{interval_ms}ms@{normalized_symbol}"
    wrapper.symbol = normalized_symbol
    if create_time_ms is not None:
        wrapper.createTime = int(create_time_ms)
    if send_time_ms is not None:
        wrapper.sendTime = int(send_time_ms)

    depth = wrapper.publicAggreDepths
    depth.eventType = f"spot@public.aggre.depth.v3.api.pb@{interval_ms}ms"
    depth.fromVersion = str(from_version)
    depth.toVersion = str(to_version)
    if last_order_create_time_ms is not None:
        depth.lastOrderCreateTime = int(last_order_create_time_ms)
    for price, quantity in asks:
        level = depth.asks.add()
        level.price = str(price)
        level.quantity = str(quantity)
    for price, quantity in bids:
        level = depth.bids.add()
        level.price = str(price)
        level.quantity = str(quantity)
    return wrapper.SerializeToString()


# Compatibility aliases for callers that use a shorter codec name.
decode_depth_frame = decode_mexc_depth_frame
encode_depth_frame = encode_mexc_depth_frame


__all__ = [
    "MEXC_PROTO_SCHEMA_COMMIT",
    "MexcProtoDepthUpdate",
    "MexcProtobufError",
    "decode_depth_frame",
    "decode_mexc_depth_frame",
    "encode_depth_frame",
    "encode_mexc_depth_frame",
    "protobuf_message_classes",
]
