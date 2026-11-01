"""Wire format of the Nivalis Coop protocol.

A frame is a 4 byte big-endian length followed by one UTF-8 JSON object with a
string field ``t`` (the packet type). A connection goes through the same
phases as a Minecraft connection: handshake (``hello``), optional status ping,
login, play. PROTOCOL.md lists every packet.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import struct
from typing import Any

PROTOCOL_VERSION = 1

MAX_FRAME_PRELOGIN = 4096
MAX_FRAME_PLAY = 256 * 1024
MAX_FRAME_FROM_SERVER = 8 * 1024 * 1024

_HEADER = struct.Struct(">I")

NAME_RE = re.compile(r"\w[\w .\-]{0,22}\w")
KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,31}(?:/[A-Za-z0-9_.\-]{1,48}){1,6}")
KIND_RE = re.compile(r"[a-z][a-z0-9_.]{0,31}")
# a game model name: letters, digits, space and a few marks, as Unity names prefabs
AVATAR_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.()\-]{0,79}")
MAX_KEY_LEN = 128


class ProtocolError(Exception):
    """The peer sent something that is not valid protocol."""


def encode(msg: dict[str, Any]) -> bytes:
    body = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return _HEADER.pack(len(body)) + body


async def read_frame(reader: asyncio.StreamReader, max_len: int) -> dict[str, Any]:
    """Read one frame; raises IncompleteReadError when the peer closed."""
    (length,) = _HEADER.unpack(await reader.readexactly(4))
    if length == 0 or length > max_len:
        raise ProtocolError(f"bad frame length {length}")
    body = await reader.readexactly(length)
    try:
        msg = json.loads(body)
    except (ValueError, RecursionError):
        raise ProtocolError("invalid json") from None
    if not isinstance(msg, dict) or not isinstance(msg.get("t"), str):
        raise ProtocolError("frame has no packet type")
    return msg


def is_number(value: Any, limit: float = 1e6) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and abs(value) <= limit
    )


def clean_text(value: Any, max_len: int) -> str | None:
    """Printable text without control characters, trimmed; None if empty/invalid."""
    if not isinstance(value, str):
        return None
    value = "".join(ch for ch in value if ch.isprintable()).strip()
    return value[:max_len] or None


def json_size(value: Any) -> int:
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and NAME_RE.fullmatch(value) is not None


def valid_key(value: Any) -> bool:
    return isinstance(value, str) and len(value) <= MAX_KEY_LEN and KEY_RE.fullmatch(value) is not None
