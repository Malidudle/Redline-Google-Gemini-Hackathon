"""Measured network egress accounting.

REDLINE claims zero cloud egress. That claim must be measured, not asserted, so this
module counts every byte that leaves the process over HTTP and classifies the
destination. Bytes to loopback are counted separately from bytes to anywhere else.

The counting hook is installed on the httpx transport classes, so any module in the
repo that uses httpx (including the redaction pipeline) is counted without changing
its code.
"""
from __future__ import annotations

import ipaddress
import threading
import typing as t

_lock = threading.Lock()

_egress_bytes = 0        # bytes sent to a NON-loopback host
_localhost_bytes = 0     # bytes sent to loopback (Ollama, mostly)
_egress_recv_bytes = 0
_localhost_recv_bytes = 0
_egress_requests = 0
_localhost_requests = 0
_egress_hosts: set[str] = set()

_LOOPBACK_NAMES = {"localhost", "localhost.", "ip6-localhost", "::1", ""}


def is_local_host(host: str | None) -> bool:
    """True when host is loopback. Anything else is treated as egress."""
    if host is None:
        return False
    h = host.strip().strip("[]").lower()
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def note_bytes(host: str | None, sent: int = 0, received: int = 0) -> None:
    """Record traffic for a destination host. Safe to call from any thread."""
    global _egress_bytes, _localhost_bytes, _egress_recv_bytes, _localhost_recv_bytes
    global _egress_requests, _localhost_requests
    local = is_local_host(host)
    with _lock:
        if local:
            _localhost_bytes += sent
            _localhost_recv_bytes += received
            if sent:
                _localhost_requests += 1
        else:
            _egress_bytes += sent
            _egress_recv_bytes += received
            if sent:
                _egress_requests += 1
            if host:
                _egress_hosts.add(host)


def get_egress_bytes() -> int:
    with _lock:
        return _egress_bytes


def get_localhost_bytes() -> int:
    with _lock:
        return _localhost_bytes


def get_stats() -> dict:
    with _lock:
        return {
            "egress_bytes": _egress_bytes,
            "egress_recv_bytes": _egress_recv_bytes,
            "egress_requests": _egress_requests,
            "egress_hosts": sorted(_egress_hosts),
            "localhost_bytes": _localhost_bytes,
            "localhost_recv_bytes": _localhost_recv_bytes,
            "localhost_requests": _localhost_requests,
        }


def reset() -> None:
    global _egress_bytes, _localhost_bytes, _egress_recv_bytes, _localhost_recv_bytes
    global _egress_requests, _localhost_requests
    with _lock:
        _egress_bytes = 0
        _localhost_bytes = 0
        _egress_recv_bytes = 0
        _localhost_recv_bytes = 0
        _egress_requests = 0
        _localhost_requests = 0
        _egress_hosts.clear()


def _request_size(request: t.Any) -> int:
    """Wire size of an httpx request: request line + headers + body."""
    try:
        url = request.url
        line = len(f"{request.method} {url.raw_path.decode('ascii', 'ignore')} HTTP/1.1\r\n")
    except Exception:
        line = 0
    headers = 0
    try:
        for k, v in request.headers.raw:
            headers += len(k) + len(v) + 4
    except Exception:
        pass
    body = 0
    try:
        body = len(request.content)
    except Exception:
        pass
    return line + headers + 2 + body


def _response_header_size(response: t.Any) -> int:
    size = 0
    try:
        for k, v in response.headers.raw:
            size += len(k) + len(v) + 4
    except Exception:
        pass
    return size + 2


_installed = False


def install_httpx_hook() -> bool:
    """Patch httpx transports so every request through httpx is measured.

    Returns True when the hook is active. Idempotent.
    """
    global _installed
    if _installed:
        return True
    try:
        import httpx
    except Exception:
        return False

    _orig_async = httpx.AsyncHTTPTransport.handle_async_request
    _orig_sync = httpx.HTTPTransport.handle_request

    class _CountingAsyncStream(httpx.AsyncByteStream):
        def __init__(self, inner, host):
            self._inner = inner
            self._host = host

        async def __aiter__(self):
            async for chunk in self._inner:
                note_bytes(self._host, received=len(chunk))
                yield chunk

        async def aclose(self):
            aclose = getattr(self._inner, "aclose", None)
            if aclose is not None:
                await aclose()

    class _CountingSyncStream(httpx.SyncByteStream):
        def __init__(self, inner, host):
            self._inner = inner
            self._host = host

        def __iter__(self):
            for chunk in self._inner:
                note_bytes(self._host, received=len(chunk))
                yield chunk

        def close(self):
            close = getattr(self._inner, "close", None)
            if close is not None:
                close()

    async def _counted_async(self, request):
        host = request.url.host
        note_bytes(host, sent=_request_size(request))
        response = await _orig_async(self, request)
        note_bytes(host, received=_response_header_size(response))
        response.stream = _CountingAsyncStream(response.stream, host)
        return response

    def _counted_sync(self, request):
        host = request.url.host
        note_bytes(host, sent=_request_size(request))
        response = _orig_sync(self, request)
        note_bytes(host, received=_response_header_size(response))
        response.stream = _CountingSyncStream(response.stream, host)
        return response

    httpx.AsyncHTTPTransport.handle_async_request = _counted_async
    httpx.HTTPTransport.handle_request = _counted_sync
    _installed = True
    return True


def is_installed() -> bool:
    return _installed
