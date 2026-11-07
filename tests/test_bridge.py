import asyncio
import copy
import ssl
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
from aiohttp import web

from ..bridge import BridgeClient, BridgeError, idempotency_key
from ..config import Settings
from ..identity import identify
from .support import Event, config, settings


def payload():
    identity = identify(Event(), settings())
    return {"actor": identity.actor, "context": identity.context, "policy": settings().policy}


@asynccontextmanager
async def server(handler):
    app = web.Application()
    app.router.add_route("*", "/{path:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    source = config()
    source["connection"].update(base_url=f"http://127.0.0.1:{port}", allow_insecure_http=True)
    client = BridgeClient(Settings.from_mapping(source))
    try:
        yield client
    finally:
        await client.close()
        await runner.cleanup()


def test_keys_stable_across_policy_updates_and_scoped_to_events():
    original = payload()
    other = copy.deepcopy(original)
    other["policy"]["robbery"]["success_probability"] = "1"
    key = idempotency_key("robbery", original)
    assert idempotency_key("robbery", other) == key
    other["context"]["message_id"] = "msg-2"
    assert idempotency_key("robbery", other) != key
    other = copy.deepcopy(original)
    other["actor"]["app_id"] = "other-bot"
    assert idempotency_key("robbery", other) != key
    assert idempotency_key("checkin", original) != key


@pytest.mark.parametrize(
    "error_type", [aiohttp.ClientConnectorCertificateError, aiohttp.ClientConnectorSSLError]
)
def test_tls_failures_do_not_retry_or_disable_certificate_validation(error_type):
    async def run():
        client = BridgeClient(settings())
        connection = SimpleNamespace(host="sub2api.example.test", port=443, is_ssl=True, ssl=True)
        session = SimpleNamespace(
            closed=False,
            post=Mock(side_effect=error_type(connection, ssl.SSLError("test handshake failure"))),
            close=AsyncMock(),
        )
        client._session = session
        try:
            with pytest.raises(BridgeError, match="TLS_ERROR"):
                await client.request("account", payload())
            session.post.assert_called_once()
            assert "ssl" not in session.post.call_args.kwargs
            assert session.post.call_args.kwargs["allow_redirects"] is False
        finally:
            await client.close()
        session.close.assert_awaited_once()

    asyncio.run(run())


def test_authenticated_post_and_session_closed():
    async def run():
        async def handler(request):
            assert request.method == "POST"
            assert request.path == "/api/v1/qq-bot/checkin"
            assert request.headers["Authorization"] == "Bearer " + settings().token
            assert request.headers["X-Bridge-Version"] == "1"
            assert request.headers["Idempotency-Key"] == idempotency_key("checkin", payload())
            assert await request.json() == payload()
            return web.json_response({"ok": True, "data": {"state": "credited"}})

        async with server(handler) as client:
            assert (await client.request("checkin", payload()))["state"] == "credited"
        assert client._session is None

    asyncio.run(run())


def test_retry_keeps_identical_body_and_idempotency_key():
    async def run():
        requests = []

        async def handler(request):
            requests.append((request.headers["Idempotency-Key"], await request.read()))
            if len(requests) == 1:
                return web.Response(status=503)
            return web.json_response({"ok": True, "data": {"state": "credited"}})

        async with server(handler) as client:
            await client.request("checkin", payload())
        assert len(requests) == 2 and requests[0] == requests[1]

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "UNAUTHORIZED"),
        (403, "UNAUTHORIZED"),
        (404, "BRIDGE_NOT_INSTALLED"),
        (302, "REDIRECT_REFUSED"),
        (429, "RATE_LIMITED"),
        (500, "UNAVAILABLE"),
    ],
)
def test_http_failures(status, code):
    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.Response(
                status=status, text="do-not-leak-token", headers={"Location": "http://127.0.0.1:1/leak"}
            )

        async with server(handler) as client:
            with pytest.raises(BridgeError, match=code) as error:
                await client.request("checkin", payload())
            assert "do-not-leak-token" not in str(error.value)
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "status,code",
    [(403, "ADMIN_REQUIRED"), (403, "APP_NOT_ALLOWED"), (404, "TARGET_NOT_FOUND")],
)
def test_http_business_errors_take_precedence_without_retry(status, code):
    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.json_response(
                {"ok": False, "error": {"code": code, "retry_after_seconds": 12}}, status=status
            )

        async with server(handler) as client:
            with pytest.raises(BridgeError) as error:
                await client.request("recharge", payload())
            assert error.value.code == code
            assert error.value.retry_after == 12
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize("status", [401, 403, 404, 429])
@pytest.mark.parametrize(
    "body",
    [
        {"ok": True, "data": {"state": "credited"}},
        {"ok": False, "error": {"code": "private-token"}},
        {"ok": False, "error": {"code": "ADMIN_REQUIRED", "retry_after_seconds": True}},
        {"ok": False, "error": []},
    ],
)
def test_invalid_error_envelopes_never_report_success(status, body):
    async def run():
        async def handler(request):
            return web.json_response(body, status=status)

        async with server(handler) as client:
            with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
                await client.request("recharge", payload())

    asyncio.run(run())


def test_business_error_is_not_retried():
    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.json_response(
                {"ok": False, "error": {"code": "TARGET_SHIELDED", "retry_after_seconds": 120}}, status=409
            )

        async with server(handler) as client:
            with pytest.raises(BridgeError) as error:
                await client.request("robbery", payload())
            assert error.value.code == "TARGET_SHIELDED" and error.value.retry_after == 120
        assert len(calls) == 1

    asyncio.run(run())


@pytest.mark.parametrize(
    "body",
    [
        "<html>502</html>",
        "{}",
        "[]",
        '{"ok": 1, "data": {}}',
        '{"ok": true, "data": []}',
        '{"ok":false,"error":{"code":"private-token"}}',
        pytest.param("x" * 70000, id="oversized-response"),
    ],
)
def test_invalid_response_rejected(body):
    async def run():
        async def handler(request):
            return web.Response(text=body)

        async with server(handler) as client:
            with pytest.raises(BridgeError, match="INVALID_RESPONSE"):
                await client.request("account", payload())

    asyncio.run(run())


def test_retry_limit():
    async def run():
        calls = []

        async def handler(request):
            calls.append(request.path)
            return web.Response(status=503)

        async with server(handler) as client:
            with pytest.raises(BridgeError, match="UNAVAILABLE"):
                await client.request("checkin", payload())
        assert len(calls) == 2

    asyncio.run(run())


def test_connection_loss_retries_same_operation():
    async def run():
        keys = []

        async def handler(request):
            keys.append(request.headers["Idempotency-Key"])
            if len(keys) == 1:
                request.transport.close()
                return web.Response()
            return web.json_response({"ok": True, "data": {"state": "credited"}})

        async with server(handler) as client:
            assert (await client.request("checkin", payload()))["state"] == "credited"
        assert len(keys) == 2 and keys[0] == keys[1]

    asyncio.run(run())


def test_committed_request_with_lost_response_is_only_applied_once():
    async def run():
        ledger = {}
        first = True

        async def handler(request):
            nonlocal first
            key = request.headers["Idempotency-Key"]
            ledger.setdefault(key, {"state": "credited", "reward": "0.10", "balance": "1.10"})
            if first:
                first = False
                return web.Response(status=504)
            return web.json_response({"ok": True, "data": ledger[key]})

        async with server(handler) as client:
            result = await client.request("checkin", payload())
        assert result["balance"] == "1.10" and len(ledger) == 1

    asyncio.run(run())
