"""Опциональный поиск og:image в публичной карточке DNS, без обхода блокировок."""
from __future__ import annotations

import logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import aiohttp

log = logging.getLogger(__name__)
_cache: dict[str, str | None] = {}


def is_dns_page(url: str) -> bool:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and (host == "dns-shop.ru" or host.endswith(".dns-shop.ru"))


class OGParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.image: str | None = None

    def handle_starttag(self, tag: str, attrs):
        if tag != "meta" or self.image:
            return
        item = dict(attrs)
        if item.get("property", "").lower() in ("og:image", "og:image:url"):
            self.image = item.get("content")


async def dns_image_url(dns_url: str) -> str | None:
    if not is_dns_page(dns_url):
        return None
    if dns_url in _cache:
        return _cache[dns_url]
    found = None
    try:
        timeout = aiohttp.ClientTimeout(total=6, connect=3)
        headers = {"User-Agent": "ComputerCatalogBot/1.0 (product preview)"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            # Не следуем автоматически переадресациям на посторонние домены.
            url = dns_url
            for _ in range(3):
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in (301, 302, 303, 307, 308):
                        next_url = urljoin(url, response.headers.get("Location", ""))
                        if not is_dns_page(next_url):
                            break
                        url = next_url
                        continue
                    if response.status != 200 or "html" not in response.headers.get("Content-Type", ""):
                        break
                    raw = await response.content.read(250_000)
                    parser = OGParser()
                    parser.feed(raw.decode("utf-8", errors="ignore"))
                    if parser.image:
                        candidate = urljoin(url, parser.image)
                        if urlsplit(candidate).scheme == "https":
                            found = candidate
                    break
    except (aiohttp.ClientError, TimeoutError, UnicodeError, ValueError) as exc:
        log.info("Фото DNS недоступно для %s: %s", dns_url, exc)
    _cache[dns_url] = found
    return found


async def product_photo(product: dict) -> str | None:
    direct = (product.get("photo_url") or "").strip()
    if direct.startswith("https://"):
        return direct
    return await dns_image_url(product.get("dns_url", ""))
