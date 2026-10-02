"""aiogram session with HTTP proxy and no aiohttp-socks (ported from v1 hub/bot/run.py: PC Telegram goes via VPN)."""

from __future__ import annotations

import asyncio

from aiogram.client.session.aiohttp import AiohttpSession


class HttpProxySession(AiohttpSession):
    """Aiohttp session with HTTP proxy and no aiohttp-socks package.

    aiogram 3.31 with any proxy= in the constructor builds a ProxyConnector
    needing aiohttp-socks (not a dependency — startup crashed
    with RuntimeError before the retry loop). Native aiohttp handles
    HTTP proxies via the request's own proxy= param — for HTTPS_PROXY
    of the http://host:port form that is enough.
    """

    def __init__(self, proxy_url: str, **kwargs) -> None:
        super().__init__(proxy=None, **kwargs)
        self._proxy_url = str(proxy_url)

    @property
    def proxy_url(self) -> str:
        """Where requests go."""
        return self._proxy_url

    async def make_request(self, bot, method, timeout=None):
        """Same as base, but proxy= goes into session.post."""
        from aiogram.exceptions import TelegramNetworkError

        session = await self.create_session()
        url = self.api.api_url(token=bot.token, method=method.__api_method__)
        form = self.build_form_data(bot=bot, method=method)
        try:
            async with session.post(
                url,
                data=form,
                proxy=self._proxy_url,
                timeout=self.timeout if timeout is None else timeout,
            ) as resp:
                raw_result = await resp.text()
        except asyncio.TimeoutError as e:
            raise TelegramNetworkError(
                method=method, message="Request timeout error") from e
        except Exception as e:  # noqa: BLE001 — same as the base ClientError branch
            from aiohttp import ClientError

            if not isinstance(e, ClientError):
                raise
            raise TelegramNetworkError(
                method=method, message=f"{type(e).__name__}: {e}") from e
        response = self.check_response(
            bot=bot,
            method=method,
            status_code=resp.status,
            content=raw_result,
        )
        return response.result

    async def stream_content(self, url, headers=None, timeout=30,
                             chunk_size=65536, raise_for_status=True):
        """Same as base, but proxy= goes into session.get."""
        if headers is None:
            headers = {}
        session = await self.create_session()
        async with session.get(
            url,
            proxy=self._proxy_url,
            timeout=timeout,
            headers=headers,
            raise_for_status=raise_for_status,
        ) as resp:
            async for chunk in resp.content.iter_chunked(chunk_size):
                yield chunk
