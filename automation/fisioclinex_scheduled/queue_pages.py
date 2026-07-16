"""Safe, bounded GitHub Pages propagation verification."""

from __future__ import annotations

import hashlib
import hmac
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit

DEFAULT_MAX_WAIT_SECONDS = 120.0
DEFAULT_RETRY_INTERVAL_SECONDS = 5.0
OFFICIAL_PAGES_BASE_URL = "https://gbpufscar.github.io/fisioclinex-instagram-assets/"
OFFICIAL_PAGES_HOST = "gbpufscar.github.io"
OFFICIAL_POSTS_PREFIX = "/fisioclinex-instagram-assets/posts/"
_TRANSIENT_STATUS_CODES = frozenset({404, 429, 500, 502, 503, 504})


class QueuePagesError(RuntimeError):
    pass


class _TransientPagesError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PageResponse:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes


def expected_slide_url(config: QueueConfig, slug: str, filename: str) -> str:
    return f"{config.pages_base_url}{quote('posts/' + slug + '/' + filename, safe='/-._~')}"


def official_slide_url(slug: str, filename: str) -> str:
    url = f"{OFFICIAL_PAGES_BASE_URL}{quote('posts/' + slug + '/' + filename, safe='/-._~')}"
    validate_official_slide_url(url, slug, filename)
    return url


def validate_official_slide_url(url: str, slug: str, filename: str) -> None:
    parsed = urlsplit(url)
    expected_path = f"{OFFICIAL_POSTS_PREFIX}{slug}/{filename}"
    if (
        parsed.scheme != "https"
        or parsed.hostname != OFFICIAL_PAGES_HOST
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != expected_path
        or "//" in parsed.path
        or ".." in parsed.path
        or "%" in parsed.path
        or url != f"https://{OFFICIAL_PAGES_HOST}{expected_path}"
    ):
        raise QueuePagesError("public slide URL is unsafe")


def _validate_expected_url(url: str, response: PageResponse) -> None:
    expected = urlsplit(url)
    requested = urlsplit(response.requested_url)
    final = urlsplit(response.final_url)
    if (
        response.requested_url != url
        or response.final_url != url
        or expected.scheme != "https"
        or requested.scheme != expected.scheme
        or requested.hostname != expected.hostname
        or requested.path != expected.path
        or final.scheme != expected.scheme
        or final.hostname != expected.hostname
        or final.path != expected.path
        or requested.query
        or requested.fragment
        or requested.username is not None
        or requested.password is not None
        or final.query
        or final.fragment
        or final.username is not None
        or final.password is not None
    ):
        raise QueuePagesError("public slide URL is invalid")


def _verify_response(url: str, response: PageResponse, local: bytes) -> None:
    _validate_expected_url(url, response)
    if response.status in _TRANSIENT_STATUS_CODES:
        raise _TransientPagesError("public slide is not propagated yet")
    if response.status != 200:
        raise QueuePagesError("public slide status is invalid")
    if response.content_type.split(";", 1)[0].strip().casefold() != "image/png":
        raise QueuePagesError("public slide content type is invalid")
    if response.body[:8] != b"\x89PNG\r\n\x1a\n" or len(response.body) < 24:
        raise QueuePagesError("public slide is not a valid PNG")
    if struct.unpack(">II", response.body[16:24]) != (1080, 1350):
        raise QueuePagesError("public slide dimensions are invalid")
    if len(local) != len(response.body) or not hmac.compare_digest(
        hashlib.sha256(local).digest(),
        hashlib.sha256(response.body).digest(),
    ):
        raise _TransientPagesError("public slide still has previous content")


def _verify_once(config: QueueConfig, package: QueuePackage, *, fetcher) -> None:
    for slide in package.slides:
        url = expected_slide_url(config, package.slug, slide.name)
        try:
            response = fetcher(url)
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise _TransientPagesError("temporary connection failure") from exc
        if not isinstance(response, PageResponse):
            raise QueuePagesError("public slide response is invalid")
        _verify_response(url, response, slide.read_bytes())


def _verify_slide_paths_once(slug: str, slides: tuple[Path, ...], *, fetcher) -> None:
    for slide in slides:
        url = official_slide_url(slug, slide.name)
        try:
            response = fetcher(url)
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise _TransientPagesError("temporary connection failure") from exc
        if not isinstance(response, PageResponse):
            raise QueuePagesError("public slide response is invalid")
        _verify_response(url, response, slide.read_bytes())


def _validate_slide_paths(slug: str, slides) -> tuple[Path, ...]:
    paths = tuple(Path(slide) for slide in slides)
    if not paths:
        raise QueuePagesError("public slide set is empty")
    expected = tuple(
        f"{slug}-slide-{number:02d}.png" for number in range(1, len(paths) + 1)
    )
    if tuple(path.name for path in paths) != expected:
        raise QueuePagesError("public slide order is invalid")
    if any(path.is_symlink() or not path.is_file() for path in paths):
        raise QueuePagesError("public slide file is invalid")
    return paths


def verify_pages(
    config: QueueConfig,
    package: QueuePackage,
    *,
    fetcher,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> bool:
    if (
        not isinstance(max_wait_seconds, (int, float))
        or isinstance(max_wait_seconds, bool)
        or max_wait_seconds < 0
        or not isinstance(retry_interval_seconds, (int, float))
        or isinstance(retry_interval_seconds, bool)
        or retry_interval_seconds <= 0
    ):
        raise QueuePagesError("Pages retry policy is invalid")
    deadline = monotonic() + float(max_wait_seconds)
    while True:
        try:
            _verify_once(config, package, fetcher=fetcher)
            return True
        except _TransientPagesError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise QueuePagesError("GitHub Pages propagation timed out") from None
            sleeper(min(float(retry_interval_seconds), remaining))


def verify_slide_paths(
    slug: str,
    slides,
    *,
    fetcher,
    max_wait_seconds: float = DEFAULT_MAX_WAIT_SECONDS,
    retry_interval_seconds: float = DEFAULT_RETRY_INTERVAL_SECONDS,
    sleeper=time.sleep,
    monotonic=time.monotonic,
) -> int:
    paths = _validate_slide_paths(slug, slides)
    if (
        not isinstance(max_wait_seconds, (int, float))
        or isinstance(max_wait_seconds, bool)
        or max_wait_seconds < 0
        or not isinstance(retry_interval_seconds, (int, float))
        or isinstance(retry_interval_seconds, bool)
        or retry_interval_seconds <= 0
    ):
        raise QueuePagesError("Pages retry policy is invalid")
    deadline = monotonic() + float(max_wait_seconds)
    while True:
        try:
            _verify_slide_paths_once(slug, paths, fetcher=fetcher)
            return len(paths)
        except _TransientPagesError:
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise QueuePagesError("GitHub Pages propagation timed out") from None
            sleeper(min(float(retry_interval_seconds), remaining))
