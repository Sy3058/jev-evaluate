"""Bounded, public-HTTPS-only checks for links cited in AI Bot answers."""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import io
import re
import socket
import ssl
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_LINKS = 8
CACHE_VERSION = 3
MAX_BYTES = 512_000
MAX_PDF_BYTES = 5_000_000
MAX_EXCERPT = 6_000
TIMEOUT_SECONDS = 4
URL_PATTERN = re.compile(r"https?://[^\s<>\"'`]+", re.I)


class PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.title_depth = 0
        self.title_parts: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden += 1
        if tag == "title":
            self.title_depth += 1
        if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "template", "svg"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "title":
            self.title_depth = max(0, self.title_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.hidden:
            return
        if self.title_depth:
            self.title_parts.append(data)
        else:
            self.parts.append(data)


def extract_citations(answer: str) -> tuple[list[dict], int]:
    found = []
    seen = set()
    for match in URL_PATTERN.finditer(answer):
        url = match.group(0).rstrip(".,;:!?)]}。〉」")
        if url in seen:
            continue
        seen.add(url)
        context = re.sub(r"\s+", " ", answer[max(0, match.start() - 220):match.start()]).strip()
        found.append({"url": url, "claimContext": context[-220:]})
    return found[:MAX_LINKS], max(0, len(found) - MAX_LINKS)


def _safe_target(url: str) -> tuple[str, int, str]:
    if len(url) > 2048 or any(c in url for c in "\\\r\n\t"):
        raise ValueError("URL 형식 차단")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("공개 HTTPS URL만 허용")
    try:
        port = parsed.port or 443
    except ValueError as error:
        raise ValueError("포트 형식 차단") from error
    if port != 443:
        raise ValueError("443 포트만 허용")
    host = parsed.hostname.rstrip(".").lower()
    if not host or "." not in host or host.endswith((".local", ".localhost", ".internal")):
        raise ValueError("내부 또는 잘못된 호스트 차단")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP 주소 직접 지정 차단")
    return host, port, urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _public_ip(host: str, port: int) -> str:
    addresses = {item[4][0] for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("비공개 또는 확인 불가 주소 차단")
    return sorted(addresses)[0]


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, ip: str, port: int) -> None:
        super().__init__(host, port=port, timeout=TIMEOUT_SECONDS, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(self.sock, server_hostname=self.host)


def _download(url: str) -> tuple[int, str, bytes, str, bool]:
    host, port, clean_url = _safe_target(url)
    ip = _public_ip(host, port)
    parsed = urlsplit(clean_url)
    connection = _PinnedHTTPSConnection(host, ip, port)
    try:
        connection.request("GET", (parsed.path or "/") + ("?" + parsed.query if parsed.query else ""),
                           headers={"Host": host, "User-Agent": "JEV-link-check/1.0",
                                    "Accept": "text/html, text/plain;q=0.9, application/pdf;q=0.8",
                                    "Accept-Encoding": "identity"})
        response = connection.getresponse()
        content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
        location = response.getheader("Location", "")
        limit = MAX_PDF_BYTES if content_type == "application/pdf" else MAX_BYTES
        body = b"" if response.status in {301, 302, 303, 307, 308} else response.read(limit + 1)
        truncated = len(body) > limit
        return response.status, content_type, body[:limit], urljoin(clean_url, location) if location else clean_url, truncated
    finally:
        connection.close()


def verify_url(url: str) -> dict:
    checked_at = datetime.now(UTC).isoformat()
    result = {"url": url, "checkerVersion": CACHE_VERSION, "checkedAt": checked_at, "status": "unavailable",
              "httpStatus": None, "finalUrl": None, "contentType": None,
              "title": None, "excerpt": None, "sha256": None, "truncated": False, "reason": None}
    current = url
    try:
        for _ in range(3):
            status, content_type, body, destination, truncated = _download(current)
            if status in {301, 302, 303, 307, 308}:
                _safe_target(destination)
                current = destination
                continue
            result.update({"httpStatus": status, "finalUrl": current, "contentType": content_type,
                           "truncated": truncated})
            if status < 200 or status >= 300:
                result["reason"] = f"HTTP {status}"
                return result
            if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/pdf"}:
                result.update({"status": "unsupported", "reason": "HTML·일반 텍스트·PDF 외 형식"})
                return result
            if content_type == "application/pdf":
                if truncated or not body.startswith(b"%PDF-"):
                    result.update({"status": "no_content", "reason": "PDF 크기 제한 초과 또는 형식 오류"})
                    return result
                from pypdf import PdfReader
                reader = PdfReader(io.BytesIO(body), strict=False)
                if reader.is_encrypted:
                    result.update({"status": "no_content", "reason": "암호화된 PDF"})
                    return result
                visible = " ".join((reader.pages[index].extract_text() or "")
                                   for index in range(min(len(reader.pages), 12)))
                visible = re.sub(r"\s+", " ", visible).strip()
                title = reader.metadata.title[:200] if reader.metadata and reader.metadata.title else None
                result["truncated"] = len(reader.pages) > 12
            elif content_type in {"text/html", "application/xhtml+xml"}:
                decoded = body.decode("utf-8", errors="replace")
                parser = PageText()
                parser.feed(decoded)
                title = re.sub(r"\s+", " ", "".join(parser.title_parts)).strip()[:200]
                visible = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
            else:
                decoded = body.decode("utf-8", errors="replace")
                title = None
                visible = re.sub(r"\s+", " ", decoded).strip()
            if len(visible) < 80:
                result.update({"status": "no_content", "reason": "읽을 수 있는 본문이 부족함"})
                return result
            result["truncated"] = result["truncated"] or len(visible) > MAX_EXCERPT
            result.update({"status": "verified", "title": title, "excerpt": visible[:MAX_EXCERPT],
                           "sha256": hashlib.sha256(body).hexdigest(),
                           "reason": "평가에 본문 일부만 전달됨" if result["truncated"] else None})
            return result
        result.update({"status": "unavailable", "reason": "리다이렉트 횟수 초과"})
    except Exception as error:
        result.update({"status": "blocked" if isinstance(error, ValueError) and "차단" in str(error) else "unavailable",
                       "reason": str(error)[:160]})
    return result
