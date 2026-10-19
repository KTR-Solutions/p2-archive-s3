"""AWS Signature Version 4 checks for path-style S3 GetObject and PutObject.

Header auth covers the body via ``x-amz-content-sha256``. A presigned URL from
the S3 client signs ``UNSIGNED-PAYLOAD`` and is checked the same way, using
the ``Host`` header the client actually sent.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from urllib.parse import quote, unquote, urlsplit

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
_MAX_SKEW = timedelta(minutes=15)
_MAX_PRESIGN = 7 * 24 * 3600
_AUTH_RE = re.compile(
    r"^AWS4-HMAC-SHA256\s+"
    r"Credential=([^,\s]+),\s*"
    r"SignedHeaders=([^,\s]+),\s*"
    r"Signature=([0-9a-fA-F]{64})\s*$"
)


@dataclass(frozen=True)
class AuthFailure(Exception):
    code: str
    message: str
    status: int = 403

    def __str__(self) -> str:
        return self.message


def remove_dot_segments(url: str) -> str:
    """RFC 3986 §5.2.4, also collapsing empty segments the way AWS does."""
    if not url:
        return ""
    output: list[str] = []
    for segment in url.split("/"):
        if segment and segment != ".":
            if segment == "..":
                if output:
                    output.pop()
            else:
                output.append(segment)
    first = "/" if url[0] == "/" else ""
    last = "/" if url[-1] == "/" and output else ""
    return first + "/".join(output) + last


def canonical_uri(path: str) -> str:
    normalized = remove_dot_segments(path) or "/"
    return quote(normalized, safe="/~")


def _query_pairs(raw_query: str) -> list[tuple[str, str]]:
    if not raw_query:
        return []
    pairs: list[tuple[str, str]] = []
    for part in raw_query.split("&"):
        if not part:
            continue
        key, sep, value = part.partition("=")
        pairs.append((key, value if sep else ""))
    return pairs


def _canonical_query(pairs: list[tuple[str, str]]) -> str:
    kept = [(key, value) for key, value in pairs if key != "X-Amz-Signature"]
    kept.sort()
    return "&".join(f"{key}={value}" for key, value in kept)


def _header_value(headers: Message, name: str) -> str:
    values = headers.get_all(name) or []
    return ",".join(" ".join(value.split()) for value in values)


def _canonical_headers(headers: Message, signed: list[str]) -> str:
    lines = [f"{name}:{_header_value(headers, name)}" for name in signed]
    return "\n".join(lines)


def _payload_line(headers: Message, *, presigned: bool) -> str:
    stated = headers.get("x-amz-content-sha256")
    if stated:
        return stated.strip()
    # S3 presigned URLs sign the literal UNSIGNED-PAYLOAD. The body is chosen
    # when the URL is later called, so it is not part of the signature.
    if presigned:
        return "UNSIGNED-PAYLOAD"
    return EMPTY_SHA256


def _signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = ("AWS4" + secret).encode("utf-8")
    for part in (date, region, service, "aws4_request"):
        key = hmac.new(key, part.encode("utf-8"), hashlib.sha256).digest()
    return key


def _parse_amz_date(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y%m%dT%H%M%SZ")
    except ValueError as exc:
        raise AuthFailure("AccessDenied", "Invalid X-Amz-Date.", 403) from exc
    return parsed.replace(tzinfo=timezone.utc)


def _query_value(pairs: list[tuple[str, str]], name: str) -> str | None:
    for key, value in pairs:
        if key == name:
            return unquote(value)
    return None


def check_time(amz_date: str, expires: int | None, now: datetime) -> None:
    stamped = _parse_amz_date(amz_date)
    if expires is None:
        if abs(now - stamped) > _MAX_SKEW:
            raise AuthFailure("RequestTimeTooSkewed", "Request time is outside the allowed window.", 403)
        return
    if expires < 0 or expires > _MAX_PRESIGN:
        raise AuthFailure("AccessDenied", "X-Amz-Expires is outside the allowed range.", 403)
    if now > stamped + timedelta(seconds=expires) + _MAX_SKEW:
        raise AuthFailure("AccessDenied", "Request has expired.", 403)
    if now + _MAX_SKEW < stamped:
        raise AuthFailure("RequestTimeTooSkewed", "Request time is outside the allowed window.", 403)


def verify(
    method: str,
    target: str,
    headers: Message,
    *,
    access_key: str,
    secret_key: str,
    region: str,
    now: datetime | None = None,
) -> None:
    """Raise ``AuthFailure`` when the request is not a valid SigV4 call."""
    parts = urlsplit(target)
    pairs = _query_pairs(parts.query)
    authorization = headers.get("authorization")
    presigned = authorization is None
    if presigned:
        signature = _query_value(pairs, "X-Amz-Signature")
        algorithm = _query_value(pairs, "X-Amz-Algorithm")
        credential = _query_value(pairs, "X-Amz-Credential")
        signed_raw = _query_value(pairs, "X-Amz-SignedHeaders")
        amz_date = _query_value(pairs, "X-Amz-Date")
        expires_raw = _query_value(pairs, "X-Amz-Expires")
        if not all([signature, algorithm, credential, signed_raw, amz_date, expires_raw]):
            raise AuthFailure("AccessDenied", "Missing presigned URL parameters.", 403)
        assert signature and algorithm and credential and signed_raw and amz_date and expires_raw
        if algorithm != "AWS4-HMAC-SHA256":
            raise AuthFailure("AccessDenied", "Unsupported signature algorithm.", 403)
        try:
            expires = int(expires_raw)
        except ValueError as exc:
            raise AuthFailure("AccessDenied", "X-Amz-Expires is not a number.", 403) from exc
    else:
        match = _AUTH_RE.match(authorization or "")
        if not match:
            raise AuthFailure("AccessDenied", "Malformed Authorization header.", 403)
        credential, signed_raw, signature = match.group(1), match.group(2), match.group(3)
        amz_date = (headers.get("x-amz-date") or "").strip()
        if not amz_date:
            raise AuthFailure("AccessDenied", "Missing x-amz-date.", 403)
        expires = None

    check_time(amz_date, expires, now or datetime.now(timezone.utc))

    credential_parts = credential.split("/")
    if len(credential_parts) != 5:
        raise AuthFailure("AccessDenied", "Malformed credential scope.", 403)
    key_id, scope_date, scope_region, service, terminator = credential_parts
    if key_id != access_key or scope_region != region or service != "s3" or terminator != "aws4_request":
        raise AuthFailure("AccessDenied", "Credential does not match this service.", 403)
    if not amz_date.startswith(scope_date):
        raise AuthFailure("AccessDenied", "Credential date does not match X-Amz-Date.", 403)

    signed = [part.strip() for part in signed_raw.split(";") if part.strip()]
    if "host" not in signed:
        raise AuthFailure("AccessDenied", "Host is not a signed header.", 403)
    payload = _payload_line(headers, presigned=presigned)
    if payload.startswith("STREAMING-"):
        raise AuthFailure(
            "InvalidRequest",
            "Streaming payload signatures are not supported.",
            400,
        )

    canonical = "\n".join(
        [
            method.upper(),
            canonical_uri(parts.path),
            _canonical_query(pairs),
            _canonical_headers(headers, signed) + "\n",
            ";".join(signed),
            payload,
        ]
    )
    scope = "/".join([scope_date, scope_region, service, terminator])
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        ]
    )
    expected = hmac.new(
        _signing_key(secret_key, scope_date, scope_region, service),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        logging.getLogger("p2_archive_s3.sigv4").debug(
            "signature mismatch\ncanonical:\n%s\nstring to sign:\n%s\nexpected %s got %s",
            canonical,
            string_to_sign,
            expected,
            signature,
        )
        raise AuthFailure("SignatureDoesNotMatch", "The request signature does not match.", 403)


def body_matches_hash(headers: Message, body: bytes) -> None:
    """Check a header-auth payload hash. Presigned calls do not send one."""
    stated = (headers.get("x-amz-content-sha256") or "").strip()
    if not stated or stated == "UNSIGNED-PAYLOAD":
        return
    digest = hashlib.sha256(body).hexdigest()
    if not hmac.compare_digest(digest, stated.lower()):
        raise AuthFailure(
            "XAmzContentSHA256Mismatch",
            "The payload hash does not match.",
            400,
        )
