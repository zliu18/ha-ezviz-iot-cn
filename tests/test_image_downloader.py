"""Tests for connection-pinned temporary image downloads."""

from __future__ import annotations

import socket
from collections.abc import Mapping
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest
from aiohttp.abc import ResolveResult
from yarl import URL

from custom_components.ezviz_iot_cn.api.errors import (
    EzvizConnectionError,
    EzvizProtocolError,
)
from custom_components.ezviz_iot_cn.api.image_downloader import (
    MAX_IMAGE_BYTES,
    EzvizImageDownloader,
    PublicOnlyResolver,
)


def _resolved(address: str) -> ResolveResult:
    return ResolveResult(
        hostname="image.example",
        host=address,
        port=443,
        family=socket.AF_INET6 if ":" in address else socket.AF_INET,
        proto=socket.IPPROTO_TCP,
        flags=0,
    )


class _FakeResolver:
    def __init__(
        self,
        results: list[ResolveResult] | None = None,
        error: OSError | None = None,
    ) -> None:
        self.results = results or []
        self.error = error
        self.calls = 0
        self.close = AsyncMock()

    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        del host, port, family
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.results


@pytest.mark.asyncio
async def test_public_only_resolver_returns_exact_public_answer_once() -> None:
    """The connector must receive the exact DNS answer that was validated."""
    answers = [_resolved("1.1.1.1"), _resolved("2606:4700:4700::1111")]
    delegate = _FakeResolver(answers)
    resolver = PublicOnlyResolver(delegate)  # type: ignore[arg-type]

    result = await resolver.resolve("image.example", 443, socket.AF_UNSPEC)

    assert result is answers
    assert delegate.calls == 1
    await resolver.close()
    delegate.close.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ["10.0.0.1"],
        ["127.0.0.1"],
        ["169.254.1.1"],
        ["224.0.0.1"],
        ["0.0.0.0"],
        ["240.0.0.1"],
        ["1.1.1.1", "10.0.0.1"],
    ],
)
async def test_public_only_resolver_rejects_every_unsafe_answer_set(
    addresses: list[str],
) -> None:
    """One unsafe address invalidates the complete DNS answer."""
    delegate = _FakeResolver([_resolved(address) for address in addresses])
    resolver = PublicOnlyResolver(delegate)  # type: ignore[arg-type]

    with pytest.raises(EzvizProtocolError, match="public addresses"):
        await resolver.resolve("image.example", 443, socket.AF_UNSPEC)

    assert delegate.calls == 1


@pytest.mark.asyncio
async def test_public_only_resolver_rejects_invalid_empty_and_failed_answers() -> None:
    """Invalid or missing DNS data cannot reach the connector."""
    invalid = _FakeResolver([_resolved("not-an-address")])
    with pytest.raises(EzvizProtocolError, match="invalid address"):
        await PublicOnlyResolver(invalid).resolve("image.example", 443)  # type: ignore[arg-type]

    empty = _FakeResolver()
    with pytest.raises(EzvizConnectionError, match="resolve"):
        await PublicOnlyResolver(empty).resolve("image.example", 443)  # type: ignore[arg-type]

    failed = _FakeResolver(error=OSError("synthetic lookup failure"))
    with pytest.raises(EzvizConnectionError, match="resolve"):
        await PublicOnlyResolver(failed).resolve("image.example", 443)  # type: ignore[arg-type]


class _FakeContent:
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def read(self, size: int = -1) -> bytes:
        return self._content if size < 0 else self._content[:size]


@dataclass(slots=True)
class _ResponseSpec:
    payload: bytes
    status: int = 200
    headers: Mapping[str, str] = field(default_factory=dict)


class _FakeResponse:
    def __init__(self, spec: _ResponseSpec) -> None:
        self.status = spec.status
        self.headers = spec.headers
        self.content = _FakeContent(spec.payload)

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> None:
        del exc_type, exc_value, traceback


class _FakeSession:
    def __init__(self, spec: _ResponseSpec | None = None) -> None:
        self.spec = spec
        self.calls: list[URL] = []
        self.closed = False
        self.close = AsyncMock(side_effect=self._mark_closed)

    def _mark_closed(self) -> None:
        self.closed = True

    def get(
        self,
        url: URL,
        *,
        timeout: object,
        allow_redirects: bool,
    ) -> _FakeResponse:
        del timeout
        assert allow_redirects is False
        if self.spec is None:
            raise AssertionError("image request was not expected")
        self.calls.append(URL(url))
        return _FakeResponse(self.spec)


@pytest.mark.asyncio
async def test_image_downloader_returns_bounded_typed_https_content() -> None:
    """Valid temporary images are returned without redirects or auth data."""
    image = b"\xff\xd8\xff\xe0synthetic-image"
    session = _FakeSession(
        _ResponseSpec(
            image,
            headers={
                "Content-Type": "image/jpeg; charset=binary",
                "Content-Length": str(len(image)),
            },
        )
    )
    downloader = EzvizImageDownloader(session)  # type: ignore[arg-type]

    result = await downloader.async_download("https://image.example/test.jpg")

    assert result == (image, "image/jpeg")
    assert session.calls == [URL("https://image.example/test.jpg")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/test.jpg",
        "https://10.10.1.12/test.jpg",
        "https://[::1]/test.jpg",
        "https://[fe80::1]/test.jpg",
    ],
)
async def test_image_downloader_rejects_private_literal_ip_before_request(
    url: str,
) -> None:
    """IP literals cannot bypass the connector resolver's public-only policy."""
    session = _FakeSession()
    downloader = EzvizImageDownloader(session)  # type: ignore[arg-type]

    with pytest.raises(EzvizProtocolError, match="public address"):
        await downloader.async_download(url)

    assert session.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "spec", "message"),
    [
        ("http://image.example/test.jpg", None, "HTTPS"),
        (
            "https://image.example/test.jpg",
            _ResponseSpec(b"x", status=500, headers={"Content-Type": "image/jpeg"}),
            "HTTP error",
        ),
        (
            "https://image.example/test.jpg",
            _ResponseSpec(
                b"x",
                headers={"Content-Type": "image/jpeg", "Content-Length": "bad"},
            ),
            "length is invalid",
        ),
        (
            "https://image.example/test.jpg",
            _ResponseSpec(
                b"not-read",
                headers={
                    "Content-Type": "image/jpeg",
                    "Content-Length": str(MAX_IMAGE_BYTES + 1),
                },
            ),
            "size limit",
        ),
        (
            "https://image.example/test.jpg",
            _ResponseSpec(b"x", headers={}),
            "type is missing",
        ),
        (
            "https://image.example/test.jpg",
            _ResponseSpec(b"x", headers={"Content-Type": "text/plain"}),
            "not an image",
        ),
    ],
)
async def test_image_downloader_rejects_invalid_transport_and_metadata(
    url: str,
    spec: _ResponseSpec | None,
    message: str,
) -> None:
    """Untrusted image metadata cannot bypass transport and memory bounds."""
    session = _FakeSession(spec)
    downloader = EzvizImageDownloader(session)  # type: ignore[arg-type]

    with pytest.raises((EzvizProtocolError, EzvizConnectionError), match=message):
        await downloader.async_download(url)


@pytest.mark.asyncio
async def test_image_downloader_rejects_body_above_limit() -> None:
    """A missing length header cannot permit an oversized body."""
    session = _FakeSession(
        _ResponseSpec(
            b"x" * (MAX_IMAGE_BYTES + 1),
            headers={"Content-Type": "image/jpeg"},
        )
    )

    with pytest.raises(EzvizProtocolError, match="size limit"):
        await EzvizImageDownloader(session).async_download(  # type: ignore[arg-type]
            "https://image.example/test.jpg"
        )


@pytest.mark.asyncio
async def test_image_downloader_closes_only_owned_session_once() -> None:
    """Config-entry cleanup owns the dedicated image session exactly once."""
    owned = _FakeSession()
    downloader = EzvizImageDownloader(owned, close_session=True)  # type: ignore[arg-type]

    await downloader.async_close()
    await downloader.async_close()

    owned.close.assert_awaited_once_with()

    borrowed = _FakeSession()
    borrowed_downloader = EzvizImageDownloader(borrowed)  # type: ignore[arg-type]
    await borrowed_downloader.async_close()
    borrowed.close.assert_not_awaited()
