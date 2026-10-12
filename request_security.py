"""Request boundaries for the local HTTP application."""
from __future__ import annotations

import io
import secrets
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

MAX_JSON_BYTES = 5_000_000
READ_TIMEOUT_SECONDS = 15
HEADER_TIMEOUT_SECONDS = 15
MAX_CONNECTIONS = 32
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


class RequestRejected(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class HeaderDeadlineReader(io.RawIOBase):
    """Apply a shared deadline to every socket read used by buffered readline."""
    def __init__(self, connection):
        super().__init__()
        self.connection = connection
        self.raw = connection.makefile("rb", buffering=0)
        self.deadline = None

    def start_headers(self) -> None:
        self.deadline = time.monotonic() + HEADER_TIMEOUT_SECONDS

    def finish_headers(self) -> None:
        self.deadline = None
        self.connection.settimeout(READ_TIMEOUT_SECONDS)

    def readable(self) -> bool:
        return True

    def readinto(self, buffer):
        if self.deadline is not None:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("HTTP header deadline exceeded")
            self.connection.settimeout(remaining)
        return self.raw.readinto(buffer)

    def close(self) -> None:
        try:
            self.raw.close()
        finally:
            super().close()


def single_header(headers, name: str, required: bool = False) -> str | None:
    values = headers.get_all(name, [])
    if len(values) > 1 or (required and len(values) != 1):
        raise RequestRejected(f"{name} 헤더가 올바르지 않습니다.")
    return values[0] if values else None


def local_origin(value: str, *, host_header: bool = False) -> tuple[str, int]:
    try:
        parsed = urlsplit("http://" + value if host_header else value)
        if (parsed.scheme != "http" or parsed.hostname not in LOCAL_HOSTS or
                parsed.username is not None or parsed.password is not None or
                parsed.path or parsed.query or parsed.fragment or
                any(c.isspace() for c in value) or "\\" in value):
            raise ValueError()
        return parsed.hostname, parsed.port or 80
    except ValueError:
        raise RequestRejected("로컬 서버의 요청 출처만 허용합니다.", 403) from None


def check_source(handler) -> None:
    host = local_origin(single_header(handler.headers, "Host", required=True), host_header=True)
    if host[1] != handler.server.server_port:
        raise RequestRejected("서버 주소가 일치하지 않습니다.", 403)
    origin = single_header(handler.headers, "Origin")
    if origin is not None and local_origin(origin) != host:
        raise RequestRejected("다른 출처의 요청은 허용하지 않습니다.", 403)


def check_token(handler) -> None:
    token = single_header(handler.headers, "X-JEV-CSRF-Token")
    if not token or not secrets.compare_digest(token.encode(), handler.csrf_token.encode()):
        raise RequestRejected("요청 토큰이 올바르지 않습니다. 화면을 새로고침해 주세요.", 403)


def read_body(handler, limit: int, content_type: str) -> bytes:
    if single_header(handler.headers, "Transfer-Encoding") is not None:
        raise RequestRejected("Transfer-Encoding 요청은 지원하지 않습니다.")
    media = single_header(handler.headers, "Content-Type", required=True)
    if media.split(";", 1)[0].strip().lower() != content_type:
        raise RequestRejected(f"{content_type} 요청만 허용합니다.", 415)
    raw_length = single_header(handler.headers, "Content-Length", required=True)
    if not raw_length or len(raw_length) > 20 or not all("0" <= c <= "9" for c in raw_length):
        raise RequestRejected("요청 본문 크기가 올바르지 않습니다.")
    length = int(raw_length)
    if length < 1:
        raise RequestRejected("빈 요청 본문은 허용하지 않습니다.")
    if length > limit:
        raise RequestRejected("요청 본문이 너무 큽니다.", 413)
    deadline = time.monotonic() + READ_TIMEOUT_SECONDS
    body = bytearray()
    try:
        while len(body) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError()
            handler.connection.settimeout(remaining)
            chunk = handler.rfile.read1(min(65536, length - len(body)))
            if not chunk:
                raise RequestRejected("요청 본문 전송이 완료되지 않았습니다.")
            body.extend(chunk)
    except TimeoutError:
        raise RequestRejected("요청 본문 읽기 시간이 초과됐습니다.", 408) from None
    finally:
        handler.connection.settimeout(READ_TIMEOUT_SECONDS)
    return bytes(body)


class LocalHTTPServer(ThreadingHTTPServer):
    """Bound connection threads; reject overload without creating another thread."""
    def __init__(self, *args, max_connections: int = MAX_CONNECTIONS, **kwargs):
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        if not self._slots.acquire(blocking=False):
            try:
                request.settimeout(1)
                request.sendall(b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()
