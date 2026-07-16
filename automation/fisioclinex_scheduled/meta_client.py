"""Minimal, non-retrying Meta Graph client for manual queue publication."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode, urlsplit

GRAPH_HOST = "graph.facebook.com"
_BUSINESS_RE = re.compile(r"^[0-9]{5,32}$")
_VERSION_RE = re.compile(r"^v[0-9]{1,2}\.[0-9]{1,2}$")
_ID_RE = re.compile(r"^[0-9]{1,64}$")


class MetaClientError(RuntimeError):
    def __init__(self, phase: str, *, http_status: int | None = None, ambiguous=False):
        super().__init__(f"Meta operation failed in phase {phase}")
        self.phase = phase
        self.http_status = http_status
        self.ambiguous = ambiguous


@dataclass(frozen=True, slots=True)
class MetaResponse:
    status: int
    final_url: str
    body: bytes = field(repr=False)


class MetaClient:
    def __init__(self, access_token: str, business_id: str, api_version: str, *, transport):
        if not isinstance(access_token, str) or not access_token:
            raise MetaClientError("configuration")
        if not _BUSINESS_RE.fullmatch(business_id or ""):
            raise MetaClientError("configuration")
        if not _VERSION_RE.fullmatch(api_version or ""):
            raise MetaClientError("configuration")
        self._token = access_token
        self.business_id = business_id
        self.api_version = api_version
        self._transport = transport

    def __repr__(self) -> str:
        return (
            f"MetaClient(business_id={self.business_id!r}, "
            f"api_version={self.api_version!r}, access_token=<redacted>)"
        )

    def _url(self, path: str) -> str:
        url = f"https://{GRAPH_HOST}/{self.api_version}/{path}"
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != GRAPH_HOST or parsed.port is not None:
            raise MetaClientError("url_validation")
        return url

    def _request(self, method: str, path: str, data: dict[str, str], phase: str) -> dict:
        url = self._url(path)
        request_url = f"{url}?{urlencode(data)}" if method == "GET" and data else url
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            response = self._transport(
                method,
                request_url,
                headers,
                b"" if method == "GET" else urlencode(data).encode(),
                20,
            )
        except (TimeoutError, ConnectionError, OSError):
            raise MetaClientError(phase, ambiguous=method == "POST") from None
        if not isinstance(response, MetaResponse) or response.final_url != request_url:
            raise MetaClientError(phase, ambiguous=method == "POST")
        if len(response.body) > 1024 * 1024:
            raise MetaClientError(phase, http_status=response.status)
        try:
            payload = json.loads(response.body)
        except (UnicodeError, json.JSONDecodeError):
            raise MetaClientError(phase, http_status=response.status) from None
        if response.status != 200 or not isinstance(payload, dict):
            raise MetaClientError(
                phase, http_status=response.status, ambiguous=method == "POST"
            )
        return payload

    def _post_id(self, path: str, data: dict[str, str], phase: str) -> str:
        payload = self._request("POST", path, data, phase)
        value = payload.get("id")
        if not isinstance(value, str) or not _ID_RE.fullmatch(value):
            raise MetaClientError(phase)
        return value

    def create_child(self, image_url: str) -> str:
        return self._post_id(
            f"{self.business_id}/media",
            {"image_url": image_url, "is_carousel_item": "true"},
            "child_container",
        )

    def create_carousel(self, children: tuple[str, ...], caption: str) -> str:
        return self._post_id(
            f"{self.business_id}/media",
            {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption},
            "carousel_container",
        )

    def publish(self, carousel_id: str) -> str:
        return self._post_id(
            f"{self.business_id}/media_publish",
            {"creation_id": carousel_id},
            "media_publish",
        )

    def wait_finished(
        self,
        container_id: str,
        *,
        sleeper=time.sleep,
        monotonic=time.monotonic,
        timeout=120.0,
        interval=5.0,
    ) -> None:
        deadline = monotonic() + timeout
        while True:
            payload = self._request(
                "GET", container_id, {"fields": "status_code"}, "container_readiness"
            )
            status = payload.get("status_code")
            if status == "FINISHED":
                return
            if status not in {"IN_PROGRESS", "PUBLISHED"}:
                raise MetaClientError("container_readiness")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise MetaClientError("container_readiness")
            sleeper(min(interval, remaining))
