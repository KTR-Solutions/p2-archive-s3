"""Path-style S3 endpoint for Excellent archive files.

GetObject calls ReadEncodedFile. PutObject calls WriteEncodedFile. Presigned
GET and PUT URLs are the same two calls with the signature in the query string.

The bucket is the division folder (``01003``). The object key ends in the
document guid. The original file name stays in the catalog and is not used here.
"""

from __future__ import annotations

import hashlib
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit
from xml.sax.saxutils import escape

from archive import DIVISION_RE, PowershellArchive, guid_from_key
from sigv4 import AuthFailure, body_matches_hash, verify

logger = logging.getLogger("p2_archive_s3")
MAX_BODY = 64 * 1024 * 1024


def content_type_for(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"%PDF"):
        return "application/pdf"
    if data.startswith(b"PK\x03\x04"):
        return "application/zip"
    return "application/octet-stream"


class ArchiveServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        store,
        access_key: str,
        secret_key: str,
        region: str,
    ) -> None:
        super().__init__(address, ArchiveHandler)
        self.store = store
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region


class ArchiveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: ArchiveServer

    def log_message(self, fmt: str, *args) -> None:
        logger.info("%s - %s", self.address_string(), fmt % args)

    def do_GET(self) -> None:  # noqa: N802
        self._handle("GET")

    def do_PUT(self) -> None:  # noqa: N802
        self._handle("PUT")

    def do_HEAD(self) -> None:  # noqa: N802
        self._handle("HEAD")

    def _handle(self, method: str) -> None:
        self.close_connection = True
        path = urlsplit(self.path).path
        if method == "GET" and path == "/health":
            self._send(200, b"ok\n", "text/plain")
            return
        try:
            verify(
                method,
                self.path,
                self.headers,
                access_key=self.server.access_key,
                secret_key=self.server.secret_key,
                region=self.server.region,
            )
            body = self._read_body() if method == "PUT" else b""
            if method == "PUT":
                body_matches_hash(self.headers, body)
            division, guid = self._object()
            if method == "PUT":
                self.server.store.write(division, guid, body)
                self._send(200, b"", "application/xml", self._etag(body))
                return
            data = self.server.store.read(division, guid)
            payload = b"" if method == "HEAD" else data
            self._send(200, payload, content_type_for(data), self._etag(data), len(data))
        except AuthFailure as exc:
            self._error(exc.status, exc.code, exc.message)
        except FileNotFoundError:
            self._error(404, "NoSuchKey", "The specified key does not exist.")
        except ValueError as exc:
            self._error(400, "InvalidArgument", str(exc))
        except Exception:
            logger.exception("archive request failed")
            self._error(500, "InternalError", "The archive call failed.")

    def _object(self) -> tuple[str, str]:
        path = urlsplit(self.path).path
        segments = [unquote(part) for part in path.split("/") if part]
        if len(segments) < 2:
            raise ValueError("object path must be /{division}/{guid}")
        division = segments[0]
        if not DIVISION_RE.fullmatch(division):
            raise ValueError("bucket must be the division folder name, for example 01003")
        return division, guid_from_key("/".join(segments[1:]))

    def _read_body(self) -> bytes:
        expect = (self.headers.get("Expect") or "").lower()
        if "100-continue" in expect:
            self.send_response_only(100)
            self.end_headers()
        length = self.headers.get("Content-Length")
        if length is not None:
            size = int(length)
            if size > MAX_BODY:
                raise ValueError("object is larger than 64 MiB")
            return self.rfile.read(size)
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return self._read_chunked()
        return b""

    def _read_chunked(self) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline()
            size = int(line.split(b";", 1)[0].strip() or b"0", 16)
            if size == 0:
                self.rfile.readline()
                break
            total += size
            if total > MAX_BODY:
                raise ValueError("object is larger than 64 MiB")
            chunks.append(self.rfile.read(size))
            self.rfile.read(2)
        return b"".join(chunks)

    def _etag(self, data: bytes) -> dict[str, str]:
        return {"ETag": '"' + hashlib.md5(data).hexdigest() + '"'}

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
        content_length: int | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) if content_length is None else content_length))
        self.send_header("Connection", "close")
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str) -> None:
        body = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f"<Error><Code>{escape(code)}</Code><Message>{escape(message)}</Message></Error>"
        ).encode("utf-8")
        self._send(status, body, "application/xml")


def serve(store, access_key: str, secret_key: str, region: str, host: str, port: int) -> ArchiveServer:
    return ArchiveServer((host, port), store, access_key, secret_key, region)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    share = os.environ["ARCHIVE_SHARE_ROOT"]
    app_dir = os.environ["ARCHIVE_APP_DIR"]
    access_key = os.environ["ARCHIVE_S3_ACCESS_KEY"]
    secret_key = os.environ["ARCHIVE_S3_SECRET_KEY"]
    region = os.environ.get("ARCHIVE_S3_REGION", "us-east-1")
    host = os.environ.get("ARCHIVE_LISTEN_HOST", "127.0.0.1")
    port = int(os.environ.get("ARCHIVE_LISTEN_PORT", "9000"))
    pwsh = os.environ.get("ARCHIVE_PWSH", "pwsh")
    store = PowershellArchive(share, app_dir, pwsh)
    try:
        httpd = serve(store, access_key, secret_key, region, host, port)
        logger.info("P2 archive S3 listening on %s:%s", host, port)
        httpd.serve_forever()
    finally:
        store.close()


if __name__ == "__main__":
    main()
