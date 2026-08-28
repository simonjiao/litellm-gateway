from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from xml.etree import ElementTree

import httpx


class STSError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class STSCredentials:
    access_key: str
    secret_key: str
    session_token: str
    expiration: str


class RustFSSTSClient:
    """Minimal SigV4 client for RustFS AssumeRole."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        *,
        region: str = "us-east-1",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint.rstrip("/"))
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RustFS STS endpoint must be an absolute HTTP(S) URL")
        self._endpoint = endpoint.rstrip("/") + "/"
        self._access_key = access_key
        self._secret_key = secret_key
        self._region = region
        self._client = client or httpx.AsyncClient(timeout=15, trust_env=False)
        self._owns_client = client is None

    async def assume_role(
        self,
        *,
        duration_seconds: int,
        policy: dict[str, Any],
        now: datetime | None = None,
    ) -> STSCredentials:
        timestamp = now or datetime.now(UTC)
        form = urlencode(
            {
                "Action": "AssumeRole",
                "Version": "2011-06-15",
                "DurationSeconds": str(duration_seconds),
                "Policy": json.dumps(policy, sort_keys=True, separators=(",", ":")),
            }
        ).encode()
        headers = self._signed_headers(form, timestamp)
        try:
            response = await self._client.post(self._endpoint, content=form, headers=headers)
        except httpx.HTTPError as exc:
            raise STSError(f"RustFS STS request failed: {exc}") from exc
        if response.status_code >= 400:
            code = _error_code(response.content)
            suffix = f" ({code})" if code is not None else ""
            raise STSError(f"RustFS STS returned HTTP {response.status_code}{suffix}")
        try:
            root = ElementTree.fromstring(response.content)
            values = {
                name: root.findtext(f".//{{*}}{name}")
                for name in ("AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration")
            }
        except ElementTree.ParseError as exc:
            raise STSError("RustFS STS returned invalid XML") from exc
        if not all(isinstance(value, str) and value for value in values.values()):
            raise STSError("RustFS STS response did not include temporary credentials")
        return STSCredentials(
            access_key=str(values["AccessKeyId"]),
            secret_key=str(values["SecretAccessKey"]),
            session_token=str(values["SessionToken"]),
            expiration=str(values["Expiration"]),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _signed_headers(self, body: bytes, timestamp: datetime) -> dict[str, str]:
        parsed = urlsplit(self._endpoint)
        host = parsed.netloc
        canonical_uri = quote(parsed.path or "/", safe="/-_.~")
        amz_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = timestamp.strftime("%Y%m%d")
        content_type = "application/x-www-form-urlencoded"
        payload_hash = hashlib.sha256(body).hexdigest()
        canonical_headers = (
            f"content-type:{content_type}\nhost:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\nx-amz-date:{amz_date}\n"
        )
        signed_headers = "content-type;host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            ["POST", canonical_uri, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{date_stamp}/{self._region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode()).hexdigest(),
            ]
        )
        signing_key = _signature_key(self._secret_key, date_stamp, self._region, "s3")
        signature = hmac.new(signing_key, string_to_sign.encode(), hashlib.sha256).hexdigest()
        authorization = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self._access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Content-Type": content_type,
            "Host": host,
            "X-Amz-Content-Sha256": payload_hash,
            "X-Amz-Date": amz_date,
            "Authorization": authorization,
        }


def workspace_session_policy(bucket: str, prefix: str, *, writable: bool) -> dict[str, Any]:
    normalized = prefix.strip("/")
    object_actions = ["s3:GetObject"]
    if writable:
        object_actions.extend(["s3:PutObject", "s3:DeleteObject"])
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:ListBucket"],
                "Resource": [f"arn:aws:s3:::{bucket}"],
                "Condition": {
                    "StringLike": {
                        "s3:prefix": [normalized, f"{normalized}/*"],
                    }
                },
            },
            {
                "Effect": "Allow",
                "Action": object_actions,
                "Resource": [f"arn:aws:s3:::{bucket}/{normalized}/*"],
            },
        ],
    }


def _signature_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    date_key = hmac.new(f"AWS4{secret_key}".encode(), date.encode(), hashlib.sha256).digest()
    region_key = hmac.new(date_key, region.encode(), hashlib.sha256).digest()
    service_key = hmac.new(region_key, service.encode(), hashlib.sha256).digest()
    return hmac.new(service_key, b"aws4_request", hashlib.sha256).digest()


def _error_code(content: bytes) -> str | None:
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    code = root.findtext(".//{*}Code") or root.findtext(".//Code")
    if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", code):
        return code
    return None
