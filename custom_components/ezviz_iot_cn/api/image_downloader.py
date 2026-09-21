"""Connection-pinned downloader for untrusted temporary image URLs."""

from __future__ import annotations

import socket
from ipaddress import ip_address
from typing import override

from aiohttp import ClientError, ClientSession, ClientTimeout, TCPConnector
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.resolver import DefaultResolver
from yarl import URL

from .errors import EzvizConnectionError, EzvizProtocolError

MAX_IMAGE_BYTES = 8 * 1024 * 1024


def _is_public_address(address: object) -> bool:
    return bool(
        getattr(address, "is_global", False)
        and not getattr(address, "is_link_local", False)
        and not getattr(address, "is_loopback", False)
        and not getattr(address, "is_multicast", False)
        and not getattr(address, "is_private", False)
        and not getattr(address, "is_reserved", False)
        and not getattr(address, "is_unspecified", False)
    )


class PublicOnlyResolver(AbstractResolver):
    """Reject a DNS answer unless every connector address is public."""

    def __init__(self, resolver: AbstractResolver) -> None:
        self._resolver = resolver

    @override
    async def resolve(
        self,
        host: str,
        port: int = 0,
        family: socket.AddressFamily = socket.AF_INET,
    ) -> list[ResolveResult]:
        try:
            results = await self._resolver.resolve(host, port, family)
        except OSError as err:
            raise EzvizConnectionError("Unable to resolve EZVIZ image host") from err
        if not results:
            raise EzvizConnectionError("Unable to resolve EZVIZ image host")
        try:
            addresses = [
                ip_address(result["host"].split("%", 1)[0]) for result in results
            ]
        except ValueError as err:
            raise EzvizProtocolError(
                "Image host resolved to an invalid address"
            ) from err
        if any(not _is_public_address(address) for address in addresses):
            raise EzvizProtocolError("Image host must resolve only to public addresses")
        return results

    @override
    async def close(self) -> None:
        await self._resolver.close()


class EzvizImageDownloader:
    """Download one temporary image through a dedicated protected session."""

    def __init__(
        self,
        session: ClientSession,
        *,
        timeout: float = 10.0,
        close_session: bool = False,
    ) -> None:
        self._session = session
        self._timeout = ClientTimeout(total=timeout)
        self._close_session = close_session

    async def async_download(self, image_url: str) -> tuple[bytes, str]:
        """Download one HTTPS image into a bounded byte buffer."""
        url = URL(image_url)
        if url.scheme != "https" or not url.host:
            raise EzvizProtocolError("Image URL must use HTTPS")
        try:
            literal_address = ip_address(url.host.split("%", 1)[0])
        except ValueError:
            pass
        else:
            if not _is_public_address(literal_address):
                raise EzvizProtocolError("Image host must use a public address")
        try:
            async with self._session.get(
                url,
                timeout=self._timeout,
                allow_redirects=False,
            ) as response:
                if response.status >= 400:
                    raise EzvizConnectionError("EZVIZ image download HTTP error")
                content_length = response.headers.get(
                    "Content-Length"
                ) or response.headers.get("content-length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as err:
                        raise EzvizProtocolError(
                            "Image content length is invalid"
                        ) from err
                    if declared_size > MAX_IMAGE_BYTES:
                        raise EzvizProtocolError("Image exceeds the 8 MiB size limit")
                content = await response.content.read(MAX_IMAGE_BYTES + 1)
                if len(content) > MAX_IMAGE_BYTES:
                    raise EzvizProtocolError("Image exceeds the 8 MiB size limit")
                content_type = response.headers.get(
                    "Content-Type"
                ) or response.headers.get("content-type")
                if content_type is None:
                    raise EzvizProtocolError("Image content type is missing")
                content_type = content_type.split(";", 1)[0].strip().lower()
                if not content_type.startswith("image/"):
                    raise EzvizProtocolError("Downloaded content is not an image")
                return content, content_type
        except (TimeoutError, ClientError) as err:
            raise EzvizConnectionError("Unable to download EZVIZ image") from err

    async def async_close(self) -> None:
        """Close the dedicated session when this downloader owns it."""
        if self._close_session and not self._session.closed:
            await self._session.close()


def create_image_downloader(*, timeout: float = 10.0) -> EzvizImageDownloader:
    """Create one config-entry-owned downloader with connection-time DNS checks."""
    resolver = PublicOnlyResolver(DefaultResolver())
    connector = TCPConnector(
        resolver=resolver,
        family=socket.AF_UNSPEC,
        ttl_dns_cache=60,
    )
    session = ClientSession(
        connector=connector,
        timeout=ClientTimeout(total=timeout),
    )
    return EzvizImageDownloader(
        session,
        timeout=timeout,
        close_session=True,
    )
