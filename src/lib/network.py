"""Debug-only HTTP diagnostics using aiohttp's public tracing API."""

import logging
from itertools import count
from time import monotonic

import aiohttp

log = logging.getLogger(__name__)


def network_trace_configs() -> list[aiohttp.TraceConfig]:
    if not log.isEnabledFor(logging.DEBUG):
        return []

    trace = aiohttp.TraceConfig()
    request_ids = count(1)

    def record(ctx, event: str, details: str = "") -> None:
        now = monotonic()
        log.debug(
            "HTTP #%d %s %s: %s elapsed=%.3fs stage=%s stage_elapsed=%.3fs%s",
            ctx.request_id,
            ctx.method,
            ctx.origin,
            event,
            now - ctx.started,
            ctx.stage,
            now - ctx.stage_started,
            details,
        )

    def stage(name: str, *, dns: bool = False):
        async def handler(session, ctx, params):
            details = f" host={params.host!r}" if dns else ""
            record(ctx, name, details)
            ctx.stage = name
            ctx.stage_started = monotonic()

        return handler

    async def start(session, ctx, params):
        ctx.request_id = next(request_ids)
        ctx.method = params.method
        # Paths can contain tokens, so log only the origin.
        ctx.origin = (
            str(params.url.origin())
            if params.url.scheme and params.url.is_absolute()
            else "<relative>"
        )
        ctx.started = ctx.stage_started = monotonic()
        ctx.stage = "request-start"
        record(ctx, "request-start")

    async def headers_sent(session, ctx, params):
        ctx.origin = str(params.url.origin())
        ctx.method = params.method
        await stage("waiting-for-response-headers")(session, ctx, params)

    def response_details(response) -> str:
        details = f" status={response.status}"
        connection = response.connection
        transport = connection.transport if connection is not None else None
        if transport is not None:
            details += f" peer={transport.get_extra_info('peername')!r}"
        return details

    async def redirect(session, ctx, params):
        record(ctx, "redirect", response_details(params.response))
        ctx.stage = "redirect-followup"
        ctx.stage_started = monotonic()

    async def end(session, ctx, params):
        record(ctx, "response-headers-received", response_details(params.response))

    async def failed(session, ctx, params):
        error = params.exception
        details = f" error={type(error).__name__}"
        if isinstance(error, aiohttp.ClientConnectorError):
            details += f" host={error.host!r} port={error.port}"
            if not isinstance(error, aiohttp.ClientConnectorCertificateError):
                details += f" errno={error.os_error.errno}"
        elif isinstance(error, aiohttp.ClientResponseError):
            details += f" status={error.status}"
        record(ctx, "request-failed", details)

    trace.on_request_start.append(start)
    trace.on_connection_queued_start.append(stage("connection-queue"))
    trace.on_connection_queued_end.append(stage("connection-queue-finished"))
    trace.on_connection_create_start.append(stage("connection-create"))
    trace.on_connection_create_end.append(stage("connection-ready"))
    trace.on_connection_reuseconn.append(stage("connection-reused"))
    trace.on_dns_resolvehost_start.append(stage("dns-resolution", dns=True))
    trace.on_dns_resolvehost_end.append(stage("dns-resolved", dns=True))
    trace.on_dns_cache_hit.append(stage("dns-cache-hit", dns=True))
    trace.on_dns_cache_miss.append(stage("dns-cache-miss", dns=True))
    trace.on_request_headers_sent.append(headers_sent)
    trace.on_request_redirect.append(redirect)
    trace.on_request_end.append(end)
    trace.on_request_exception.append(failed)
    return [trace]
