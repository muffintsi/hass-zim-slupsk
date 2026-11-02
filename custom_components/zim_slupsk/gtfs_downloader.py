"""Bounded conditional downloads through HA's shared HTTP session."""

import asyncio
import json
import logging
import os
from html.parser import HTMLParser
from pathlib import Path
from time import monotonic, time
from urllib.parse import urljoin, urlparse

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import GTFS_URL
from .gtfs_parser import GTFSParser, async_executor_job

_LOGGER = logging.getLogger(__name__)
_TIMEOUT = aiohttp.ClientTimeout(total=60, connect=10, sock_read=30)
_MAX_DOWNLOAD = 32 * 1024 * 1024


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.href = None
        self.label = ""

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = ""

    def handle_data(self, data):
        if self.href:
            self.label += data

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append((self.href, self.label.strip()))
            self.href = None


def _find_url(html):
    parser = _Links()
    parser.feed(html)
    links = sorted(
        parser.links,
        key=lambda item: not (item[1].casefold() == "tym linkiem" or "gtfs" in item[1].casefold()),
    )
    for href, label in links:
        if label.casefold() == "tym linkiem" or urlparse(href).path.lower().endswith(".zip"):
            url = urljoin(GTFS_URL, href)
            if urlparse(url).scheme in ("http", "https"):
                return url
    raise ValueError("No GTFS download link found")


def _read_metadata(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _commit(path, payload, metadata):
    """Validate before replacing the working copy; never extract archive paths."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".zip.tmp")
    try:
        temp.write_bytes(payload)
        if not GTFSParser(str(temp))._load_data():
            raise ValueError("Downloaded GTFS is invalid")
        os.replace(temp, path)
        meta = path.with_name("gtfs_metadata.json")
        temp_meta = meta.with_suffix(".json.tmp")
        try:
            temp_meta.write_text(json.dumps(metadata), encoding="utf-8")
            os.replace(temp_meta, meta)
        except OSError:
            _LOGGER.warning("GTFS saved, but metadata could not be written", exc_info=True)
        finally:
            temp_meta.unlink(missing_ok=True)
    finally:
        temp.unlink(missing_ok=True)


class GTFSDownloader:
    """A single instance per HA, serialized by the feed store."""

    def __init__(self, hass, path):
        self.hass = hass
        self.path = Path(path)
        self.http_requests = 0
        self.next_check = 0.0

    async def _read_response(self, response, limit):
        payload = bytearray()
        async for chunk in response.content.iter_chunked(65536):
            payload.extend(chunk)
            if len(payload) > limit:
                raise ValueError("GTFS HTTP response exceeds size limit")
        return bytes(payload)

    async def async_download(self, force_update=False):
        exists = await self.hass.async_add_executor_job(self.path.is_file)
        if not force_update and monotonic() < self.next_check:
            return exists, False
        session = async_get_clientsession(self.hass)
        before = self.http_requests
        try:
            metadata = await self.hass.async_add_executor_job(
                _read_metadata, self.path.with_name("gtfs_metadata.json")
            )
            # After restart the file mtime is a safe fallback for old metadata.
            if exists and not force_update:
                age = await self.hass.async_add_executor_job(
                    lambda: time() - self.path.stat().st_mtime
                )
                if age < 86400:
                    self.next_check = monotonic() + max(60, 86400 - age)
                    return True, False
            self.http_requests += 1
            async with session.get(GTFS_URL, timeout=_TIMEOUT) as response:
                response.raise_for_status()
                html = (await self._read_response(response, 2 * 1024 * 1024)).decode("utf-8")
            url = await self.hass.async_add_executor_job(_find_url, html)
            headers = {}
            if exists and metadata.get("url") == url:
                if metadata.get("etag"):
                    headers["If-None-Match"] = metadata["etag"]
                if metadata.get("last_modified"):
                    headers["If-Modified-Since"] = metadata["last_modified"]
            self.http_requests += 1
            async with session.get(url, headers=headers, timeout=_TIMEOUT) as response:
                if response.status == 304 and exists:
                    await self.hass.async_add_executor_job(self.path.touch)
                    self.next_check = monotonic() + 86400
                    return True, False
                response.raise_for_status()
                payload = await self._read_response(response, _MAX_DOWNLOAD)
                metadata = {
                    **metadata,
                    "url": url,
                    "etag": response.headers.get("ETag"),
                    "last_modified": response.headers.get("Last-Modified"),
                }
            await async_executor_job(self.hass, _commit, self.path, payload, metadata)
            self.next_check = monotonic() + 86400
            return True, True
        except (
            aiohttp.ClientError,
            TimeoutError,
            asyncio.TimeoutError,
            OSError,
            ValueError,
        ) as err:
            self.next_check = monotonic() + 3600
            _LOGGER.warning("GTFS update failed; local copy available=%s: %s", exists, err)
            return exists, False
        finally:
            _LOGGER.debug(
                "GTFS refresh HTTP requests=%d total=%d",
                self.http_requests - before,
                self.http_requests,
            )
