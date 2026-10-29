import asyncio
import logging
import socket
import ssl
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer
from yarl import URL

from src.lib.network import network_trace_configs

LOGGER = "src.lib.network"
SECRET_URL = URL("https://user:password@example.test/token-path?key=secret#fragment")


class FailingResolver(aiohttp.abc.AbstractResolver):
    async def resolve(self, host, port=0, family=socket.AF_INET):
        raise socket.gaierror(socket.EAI_NONAME, "secret resolver message")

    async def close(self):
        pass


class TestNetworkTrace(unittest.IsolatedAsyncioTestCase):
    def test_normal_logging_does_not_install_tracing(self):
        with self.assertNoLogs(LOGGER, logging.INFO):
            with patch.object(logging.getLogger(LOGGER), "level", logging.INFO):
                self.assertEqual(network_trace_configs(), [])

    async def test_timeout_stages_and_redaction(self):
        for signal, stage in (
            ("on_connection_queued_start", "connection-queue"),
            ("on_connection_create_start", "connection-create"),
            ("on_dns_resolvehost_start", "dns-resolution"),
            ("on_request_headers_sent", "waiting-for-response-headers"),
        ):
            with self.subTest(stage=stage), self.assertLogs(LOGGER, "DEBUG") as logs:
                trace = network_trace_configs()[0]
                trace.freeze()
                ctx = trace.trace_config_ctx()
                params = SimpleNamespace(
                    method="GET",
                    url=SECRET_URL,
                    host="example.test",
                    headers={"Authorization": "secret-header"},
                    exception=asyncio.TimeoutError("secret exception"),
                )
                with patch("src.lib.network.monotonic", return_value=10):
                    await trace.on_request_start.send(None, ctx, params)
                with patch("src.lib.network.monotonic", return_value=12):
                    await getattr(trace, signal).send(None, ctx, params)
                with patch("src.lib.network.monotonic", return_value=15):
                    await trace.on_request_exception.send(None, ctx, params)
                output = "\n".join(logs.output)
                self.assertIn("HTTP #1 GET https://example.test", output)
                self.assertIn(
                    f"request-failed elapsed=5.000s stage={stage} stage_elapsed=3.000s",
                    output,
                )
                self.assertIn("error=TimeoutError", output)
                for secret in (
                    "user",
                    "password",
                    "token-path",
                    "key",
                    "secret",
                    "fragment",
                ):
                    self.assertNotIn(secret, output)

    async def test_real_dns_failure(self):
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            connector = aiohttp.TCPConnector(resolver=FailingResolver())
            async with aiohttp.ClientSession(
                connector=connector, trace_configs=network_trace_configs()
            ) as session:
                with self.assertRaises(aiohttp.ClientConnectorDNSError):
                    await session.get(SECRET_URL)
        output = "\n".join(logs.output)
        self.assertIn("stage=dns-resolution", output)
        self.assertIn("error=ClientConnectorDNSError", output)
        self.assertIn(f"host='example.test' port=443 errno={socket.EAI_NONAME}", output)
        self.assertNotIn("secret", output)
        self.assertNotIn("password", output)

    async def test_connection_refused(self):
        # Reserve a loopback port without listening; no DNS or external network.
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            with self.assertLogs(LOGGER, "DEBUG") as logs:
                async with aiohttp.ClientSession(
                    trace_configs=network_trace_configs()
                ) as session:
                    with self.assertRaises(aiohttp.ClientConnectorError) as caught:
                        await session.get(f"http://127.0.0.1:{port}/secret")
        output = "\n".join(logs.output)
        self.assertIn("stage=connection-create", output)
        self.assertIn(f"host='127.0.0.1' port={port}", output)
        self.assertIn(f"errno={caught.exception.os_error.errno}", output)
        self.assertNotIn("secret", output)

    async def test_certificate_failure(self):
        error = aiohttp.ClientConnectorCertificateError(
            SimpleNamespace(host="example.test", port=443, is_ssl=True),
            ssl.CertificateError("secret certificate message"),
        )
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            trace = network_trace_configs()[0]
            trace.freeze()
            ctx = trace.trace_config_ctx()
            params = SimpleNamespace(method="GET", url=SECRET_URL, exception=error)
            await trace.on_request_start.send(None, ctx, params)
            await trace.on_connection_create_start.send(None, ctx, params)
            await trace.on_request_exception.send(None, ctx, params)
        self.assertIn("error=ClientConnectorCertificateError", logs.output[-1])
        self.assertIn("host='example.test' port=443", logs.output[-1])
        self.assertNotIn("secret", "\n".join(logs.output))

    async def test_interleaved_requests_have_independent_state(self):
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            trace = network_trace_configs()[0]
            trace.freeze()
            first, second = trace.trace_config_ctx(), trace.trace_config_ctx()
            params = SimpleNamespace(
                method="GET", url=SECRET_URL, exception=asyncio.CancelledError()
            )
            with patch("src.lib.network.monotonic", return_value=10):
                await trace.on_request_start.send(None, first, params)
                await trace.on_connection_queued_start.send(None, first, params)
            with patch("src.lib.network.monotonic", return_value=12):
                await trace.on_request_start.send(None, second, params)
                await trace.on_connection_create_start.send(None, second, params)
            with patch("src.lib.network.monotonic", return_value=15):
                await trace.on_request_exception.send(None, first, params)
                await trace.on_request_exception.send(None, second, params)
        self.assertIn("HTTP #1", logs.output[-2])
        self.assertIn("elapsed=5.000s stage=connection-queue", logs.output[-2])
        self.assertIn("HTTP #2", logs.output[-1])
        self.assertIn("elapsed=3.000s stage=connection-create", logs.output[-1])
        self.assertIn("error=CancelledError", logs.output[-1])

    async def test_redirect_reuse_and_http_failure(self):
        async def redirect(request):
            raise web.HTTPFound("/secret-path?token=secret")

        async def ok(request):
            return web.Response(text="secret body")

        async def failure(request):
            return web.Response(status=503, reason="secret reason")

        app = web.Application()
        app.router.add_get("/redirect", redirect)
        app.router.add_get("/secret-path", ok)
        app.router.add_get("/failure", failure)
        async with TestServer(app) as server:
            with self.assertLogs(LOGGER, "DEBUG") as logs:
                async with aiohttp.ClientSession(
                    trace_configs=network_trace_configs(), raise_for_status=True
                ) as session:
                    async with session.get(server.make_url("/redirect")) as response:
                        self.assertEqual(await response.text(), "secret body")
                    with self.assertRaises(aiohttp.ClientResponseError):
                        await session.get(server.make_url("/failure"))
        output = "\n".join(logs.output)
        self.assertIn("redirect", output)
        self.assertIn("status=302", output)
        self.assertIn("connection-reused", output)
        self.assertIn("response-headers-received", output)
        self.assertIn("HTTP #2", output)
        self.assertIn("error=ClientResponseError status=503", output)
        self.assertNotIn("secret", output)

    async def test_available_ipv6_peer_and_released_connection(self):
        with self.assertLogs(LOGGER, "DEBUG") as logs:
            trace = network_trace_configs()[0]
            trace.freeze()
            ctx = trace.trace_config_ctx()
            transport = SimpleNamespace(
                get_extra_info=lambda key: ("2001:db8::1", 443, 0, 0)
            )
            response = SimpleNamespace(
                status=200, connection=SimpleNamespace(transport=transport)
            )
            params = SimpleNamespace(method="GET", url=SECRET_URL, response=response)
            await trace.on_request_start.send(None, ctx, params)
            await trace.on_request_end.send(None, ctx, params)
            response.connection = None
            await trace.on_request_end.send(None, ctx, params)
        self.assertIn("peer=('2001:db8::1', 443, 0, 0)", logs.output[-2])
        self.assertNotIn("peer=", logs.output[-1])
