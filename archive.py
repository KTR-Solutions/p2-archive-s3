"""Call ReadEncodedFile / WriteEncodedFile and map an S3 key onto the share."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger("p2_archive_s3")

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
DIVISION_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
WORKER_PS1 = Path(__file__).with_name("worker.ps1")


class ArchiveError(Exception):
    pass


def guid_from_key(key: str) -> str:
    parts = [part for part in key.split("/") if part and part != "."]
    if any(part == ".." for part in parts):
        raise ValueError("object key must not contain '..'")
    if not parts or not GUID_RE.fullmatch(parts[-1]):
        raise ValueError("object key must end in a document guid")
    return parts[-1]


def archive_path(share_root: str, division: str, guid: str) -> Path:
    if not DIVISION_RE.fullmatch(division):
        raise ValueError("bucket must be the division folder name, for example 01003")
    if not GUID_RE.fullmatch(guid):
        raise ValueError("object key must end in a document guid")
    root = Path(share_root)
    path = root / "Excellent" / division / "Archiv" / guid
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError("archive path escapes the share root") from exc
    return path


class MemoryStore:
    """In-process stand-in used by tests. Keys are (division, guid)."""

    def __init__(self) -> None:
        self.files: dict[tuple[str, str], bytes] = {}

    def read(self, division: str, guid: str) -> bytes:
        guid_from_key(guid)
        if not DIVISION_RE.fullmatch(division):
            raise ValueError("bucket must be the division folder name, for example 01003")
        try:
            return self.files[(division, guid)]
        except KeyError as exc:
            raise FileNotFoundError(guid) from exc

    def write(self, division: str, guid: str, data: bytes) -> None:
        guid_from_key(guid)
        if not DIVISION_RE.fullmatch(division):
            raise ValueError("bucket must be the division folder name, for example 01003")
        self.files[(division, guid)] = data

    def close(self) -> None:
        return None


class PowershellArchive:
    """Long-running PowerShell 7 worker. Assemblies stay loaded between calls."""

    def __init__(self, share_root: str, app_dir: str, pwsh: str = "pwsh") -> None:
        self.share_root = share_root
        self.tmp = Path(tempfile.mkdtemp(prefix="p2-archive-"))
        env = os.environ.copy()
        env["ARCHIVE_APP_DIR"] = app_dir
        env["PYTHONIOENCODING"] = "utf-8"
        self.proc = subprocess.Popen(
            [pwsh, "-NoProfile", "-NonInteractive", "-File", str(WORKER_PS1)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._stderr = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr.start()
        ready = self._recv()
        if not ready.get("ready"):
            detail = ready.get("error") or "worker did not become ready"
            self.close()
            raise ArchiveError(detail)

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for line in self.proc.stderr:
            logger.info("worker: %s", line.rstrip())

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        assert self.proc.stdout is not None
        line = self.proc.stdout.readline()
        if not line:
            raise ArchiveError("archive worker exited")
        reply = json.loads(line)
        if not isinstance(reply, dict):
            raise ArchiveError("archive worker returned a non-object")
        return reply

    def read(self, division: str, guid: str) -> bytes:
        path = archive_path(self.share_root, division, guid)
        if not path.is_file():
            raise FileNotFoundError(str(path))
        out = self.tmp / f"read-{guid}"
        with self._lock:
            self._send({"op": "read", "path": str(path), "file": str(out)})
            reply = self._recv()
            try:
                if not reply.get("ok"):
                    raise ArchiveError(str(reply.get("error") or "ReadEncodedFile failed"))
                return out.read_bytes()
            finally:
                out.unlink(missing_ok=True)

    def write(self, division: str, guid: str, data: bytes) -> None:
        archive_path(self.share_root, division, guid)
        incoming = self.tmp / f"write-{guid}"
        with self._lock:
            incoming.write_bytes(data)
            try:
                self._send({"op": "write", "guid": guid, "file": str(incoming)})
                reply = self._recv()
            finally:
                incoming.unlink(missing_ok=True)
        if not reply.get("ok"):
            raise ArchiveError(str(reply.get("error") or "WriteEncodedFile failed"))

    def close(self) -> None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except Exception:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
