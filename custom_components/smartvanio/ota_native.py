"""Minimal async ESPHome OTA v2 client (passwordless).

Used as a fallback for devices running firmware that predates the MQTT OTA
subscription (`{device_name}/ota/update`). The ESPHome OTA listener on TCP
port 3232 is always present in ESPHome-built firmware, so pushing a new
binary directly over TCP works regardless of firmware age — as long as the
device is reachable on the network and has no OTA password set.

Protocol reference: esphome/esphome espota2.py (passwordless, no-compression path).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
from typing import Callable

_LOGGER = logging.getLogger(__name__)

MAGIC_BYTES = bytes([0x6C, 0x26, 0xF7, 0x5C, 0x45])

RESPONSE_OK = 0x00
RESPONSE_REQUEST_AUTH = 0x01
RESPONSE_REQUEST_SHA256_AUTH = 0x02
RESPONSE_HEADER_OK = 0x40
RESPONSE_AUTH_OK = 0x41
RESPONSE_UPDATE_PREPARE_OK = 0x42
RESPONSE_BIN_MD5_OK = 0x43
RESPONSE_RECEIVE_OK = 0x44
RESPONSE_UPDATE_END_OK = 0x45
RESPONSE_SUPPORTS_COMPRESSION = 0x46
RESPONSE_CHUNK_OK = 0x47

OTA_VERSION_1_0 = 1
OTA_VERSION_2_0 = 2

CHUNK_SIZE = 8192
CONNECT_TIMEOUT = 20
READ_TIMEOUT = 90


class OTAError(Exception):
    """ESPHome OTA native flow failed."""


async def _read(reader: asyncio.StreamReader, n: int) -> bytes:
    return await asyncio.wait_for(reader.readexactly(n), timeout=READ_TIMEOUT)


async def _expect(
    reader: asyncio.StreamReader, expected: int, stage: str
) -> None:
    resp = (await _read(reader, 1))[0]
    if resp != expected:
        raise OTAError(
            f"stage '{stage}': expected 0x{expected:02X}, got 0x{resp:02X}"
        )


async def run_ota(
    host: str,
    binary: bytes,
    *,
    progress: Callable[[int], None] | None = None,
    port: int = 3232,
) -> None:
    """Push `binary` to an ESPHome device at host:port via native OTA v2/v1.

    Raises OTAError on any protocol failure. Device reboots into new firmware
    after successful completion.
    """
    size = len(binary)
    md5 = hashlib.md5(binary).hexdigest()

    _LOGGER.info(
        "Native OTA → %s:%d: %d bytes, md5=%s", host, port, size, md5
    )

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port), timeout=CONNECT_TIMEOUT
    )

    # Ensure low latency for the small handshake packets.
    sock: socket.socket = writer.get_extra_info("socket")
    if sock is not None:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    try:
        # 1. Magic bytes → expect RESPONSE_OK + version byte
        writer.write(MAGIC_BYTES)
        await writer.drain()

        resp = await _read(reader, 2)
        if resp[0] != RESPONSE_OK:
            raise OTAError(f"magic rejected: 0x{resp[0]:02X}")
        ota_version = resp[1]
        if ota_version not in (OTA_VERSION_1_0, OTA_VERSION_2_0):
            raise OTAError(f"unsupported OTA version {ota_version}")

        # 2. Features — advertise none (no compression, no sha256 auth)
        writer.write(bytes([0x00]))
        await writer.drain()
        features_resp = (await _read(reader, 1))[0]
        if features_resp not in (RESPONSE_HEADER_OK, RESPONSE_SUPPORTS_COMPRESSION):
            raise OTAError(f"features rejected: 0x{features_resp:02X}")

        # 3. Auth — fallback only supports passwordless devices
        auth_resp = (await _read(reader, 1))[0]
        if auth_resp in (RESPONSE_REQUEST_AUTH, RESPONSE_REQUEST_SHA256_AUTH):
            raise OTAError("device requires OTA password (unsupported in fallback)")
        if auth_resp != RESPONSE_AUTH_OK:
            raise OTAError(f"unexpected auth response: 0x{auth_resp:02X}")

        # 4. Size
        writer.write(size.to_bytes(4, "big"))
        await writer.drain()
        await _expect(reader, RESPONSE_UPDATE_PREPARE_OK, "size")

        # 5. MD5 as 32 ASCII hex chars
        writer.write(md5.encode("ascii"))
        await writer.drain()
        await _expect(reader, RESPONSE_BIN_MD5_OK, "md5")

        # 6. Chunked upload — disable Nagle off; enlarge send buffer.
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 65536)

        sent = 0
        last_pct_reported = -1
        for offset in range(0, size, CHUNK_SIZE):
            chunk = binary[offset : offset + CHUNK_SIZE]
            writer.write(chunk)
            await writer.drain()
            if ota_version == OTA_VERSION_2_0:
                await _expect(reader, RESPONSE_CHUNK_OK, "chunk")
            sent += len(chunk)

            if progress is not None:
                pct = int(sent * 100 / size)
                if pct != last_pct_reported:
                    last_pct_reported = pct
                    progress(pct)

        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        # 7. End-of-transfer acks
        await _expect(reader, RESPONSE_RECEIVE_OK, "receive")
        await _expect(reader, RESPONSE_UPDATE_END_OK, "end")

        # 8. Send our end ack, then let the device reboot.
        writer.write(bytes([RESPONSE_OK]))
        await writer.drain()

        _LOGGER.info("Native OTA to %s complete (%d bytes)", host, size)

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass
