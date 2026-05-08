"""HTTP firmware upload for ESPHome devices with the `web_server` component.

ESPHome's web_server exposes `POST /update` accepting `multipart/form-data`
with a single field `update` whose body is the firmware binary. The form is
the same one served by the device's web UI (Manual update).

This is the preferred path for legacy-firmware rescue flashes because:
  - Goes through HTTP/80 (same port as `/provision`), avoiding firewall rules
    that may block the native OTA TCP port 3232.
  - No protocol implementation needed — it's a plain multipart upload.

Falls back to ota_native.py for firmwares built without the web_server
component (where /update returns 404).
"""

from __future__ import annotations

import logging
from typing import Callable

import aiohttp

_LOGGER = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 180


class HTTPOTAError(Exception):
    """HTTP firmware upload failed."""


async def run_http_ota(
    session: aiohttp.ClientSession,
    host: str,
    binary: bytes,
    *,
    progress: Callable[[int], None] | None = None,
) -> None:
    """POST `binary` to http://{host}/update as multipart/form-data.

    Raises HTTPOTAError on non-2xx response or transport failure. The device
    reboots into the new firmware after a successful upload; verification
    that the new firmware actually came up is the caller's responsibility
    (status topic / config payload arriving with the new version).
    """
    url = f"http://{host}/update"
    size = len(binary)
    _LOGGER.info("HTTP OTA → %s: %d bytes", url, size)

    if progress is not None:
        progress(0)

    form = aiohttp.FormData()
    form.add_field(
        "update",
        binary,
        filename="firmware.bin",
        content_type="application/octet-stream",
    )

    timeout = aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)
    try:
        async with session.post(url, data=form, timeout=timeout) as resp:
            body = await resp.text()
            if resp.status != 200:
                raise HTTPOTAError(
                    f"HTTP {resp.status} from {url}: {body[:200]}"
                )
            _LOGGER.info(
                "HTTP OTA to %s complete (%d bytes, response: %r)",
                host, size, body[:80],
            )
    except aiohttp.ClientError as err:
        raise HTTPOTAError(f"transport error: {err}") from err

    if progress is not None:
        progress(100)


async def has_update_endpoint(
    session: aiohttp.ClientSession, host: str
) -> bool:
    """Probe whether the device's web_server exposes /update.

    Returns True for any non-404 response (200, 405, 500 with empty body all
    indicate the route is registered — only 404 means web_server is built
    without OTA support).
    """
    url = f"http://{host}/update"
    try:
        async with session.get(url, timeout=5) as resp:
            return resp.status != 404
    except Exception:  # noqa: BLE001
        return False
