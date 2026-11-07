"""The bridge is authoritative for bindings, randomness, locks and balances."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import aiohttp

from .config import Settings

MAX_RESPONSE_BYTES = 65536
ROUTES = {"bind": "/bindings/request", "account": "/account", "checkin": "/checkin", "robbery": "/robbery"}


class BridgeError(Exception):
    def __init__(self, code: str, retry_after: int = 0):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after


def idempotency_key(action: str, payload: Mapping) -> str:
    # Exclude mutable policy and credentials: a redelivered QQ event remains the same operation.
    context = payload["context"]
    identity = [1, action, payload["actor"], context.get("group_openid", ""), context["message_id"]]
    data = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode()).hexdigest()


def protocol_error() -> BridgeError:
    return BridgeError("INVALID_RESPONSE")


class BridgeClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(8)

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def request(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        if action not in ROUTES:
            raise ValueError("Unsupported bridge action")
        body = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        headers = {
            "Authorization": f"Bearer {self.settings.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Bridge-Version": "1",
            "Idempotency-Key": idempotency_key(action, payload),
        }
        async with self._semaphore:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=self.settings.timeout_seconds),
                    trust_env=False,
                )
            # At most one retry with byte-identical body/key; never retry business errors.
            for attempt in range(2):
                try:
                    url = self.settings.base_url + "/api/v1/qq-bot" + ROUTES[action]
                    async with self._session.post(
                        url, data=body, headers=headers, allow_redirects=False
                    ) as response:
                        if response.status in {502, 503, 504} and attempt == 0:
                            await asyncio.sleep(0.2)
                            continue
                        if response.status in {401, 403}:
                            raise BridgeError("UNAUTHORIZED")
                        if 300 <= response.status < 400:
                            raise BridgeError("REDIRECT_REFUSED")
                        if response.status == 404:
                            raise BridgeError("BRIDGE_NOT_INSTALLED")
                        if response.status >= 500:
                            raise BridgeError("UNAVAILABLE")
                        chunks = bytearray()
                        async for chunk in response.content.iter_chunked(8192):
                            chunks.extend(chunk)
                            if len(chunks) > MAX_RESPONSE_BYTES:
                                raise protocol_error()
                        try:
                            result = json.loads(chunks)
                        except (ValueError, UnicodeError) as exc:
                            if response.status == 429:
                                raise BridgeError("RATE_LIMITED") from exc
                            raise protocol_error() from exc
                        if not isinstance(result, dict):
                            raise protocol_error()
                        if result.get("ok") is False:
                            error = result.get("error")
                            if not isinstance(error, dict) or not isinstance(error.get("code"), str):
                                raise protocol_error()
                            code = error["code"]
                            if (
                                len(code) > 64
                                or not all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ_" for c in code)
                                or not code
                            ):
                                raise protocol_error()
                            seconds = error.get("retry_after_seconds", 0)
                            if type(seconds) is not int or not 0 <= seconds <= 31536000:
                                raise protocol_error()
                            raise BridgeError(code, seconds)
                        if response.status == 429:
                            raise BridgeError("RATE_LIMITED")
                        if not 200 <= response.status < 300 or result.get("ok") is not True:
                            raise protocol_error()
                        if not isinstance(result.get("data"), dict):
                            raise protocol_error()
                        return result["data"]
                except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                    if attempt == 1:
                        raise BridgeError("UNAVAILABLE") from exc
                    await asyncio.sleep(0.2)
        raise BridgeError("UNAVAILABLE")
