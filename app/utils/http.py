"""Polite HTTP client: timeouts, retries with backoff, per-host rate limiting
and robots.txt checks. It never spoofs a browser and never tries to get past
bot protection: a 403/CAPTCHA page is treated as "not permitted" and reported.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Callable
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class BlockedError(RuntimeError):
    """Site refused automated access (403, CAPTCHA page, robots.txt)."""


class PoliteClient:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20,
        max_retries: int = 3,
        per_host_delay: float = 2.0,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.per_host_delay = per_host_delay
        self._sleep = sleep
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "en,de;q=0.8"},
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=True,
            transport=transport,
        )

    # ------------------------------------------------------------------ rate limit
    def _wait_for_host(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.per_host_delay - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_hit[host] = time.monotonic()

    # ------------------------------------------------------------------ robots.txt
    def allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        if base not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                resp = self._request("GET", base + "/robots.txt", check_robots=False, retries=1)
                if resp.status_code >= 400:
                    # No robots.txt (404) = allowed; 401/403 = treat as disallowed.
                    rp = None if resp.status_code in (404, 410) else _deny_all()
                else:
                    rp.parse(resp.text.splitlines())
            except Exception as exc:  # network trouble: be conservative
                log.warning("robots.txt unavailable for %s: %s", base, exc)
                rp = _deny_all()
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch(self.user_agent, url)

    # ------------------------------------------------------------------ requests
    def get(self, url: str, *, check_robots: bool = True, **kw) -> httpx.Response:
        return self._request("GET", url, check_robots=check_robots, **kw)

    def post(self, url: str, *, check_robots: bool = False, **kw) -> httpx.Response:
        return self._request("POST", url, check_robots=check_robots, **kw)

    def _request(self, method: str, url: str, *, check_robots: bool, retries: int | None = None, **kw) -> httpx.Response:
        if check_robots and not self.allowed(url):
            raise BlockedError(f"robots.txt disallows {url}")
        host = urlsplit(url).netloc
        attempts = (retries if retries is not None else self.max_retries)
        last_exc: Exception | None = None
        for attempt in range(1, attempts + 1):
            self._wait_for_host(host)
            try:
                resp = self._client.request(method, url, **kw)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning("%s %s failed (attempt %d/%d): %s", method, url, attempt, attempts, exc)
            else:
                if resp.status_code == 403 or _looks_like_challenge(resp):
                    raise BlockedError(f"{url} refused automated access (HTTP {resp.status_code})")
                if resp.status_code not in RETRY_STATUSES:
                    return resp
                last_exc = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                log.warning("%s %s -> HTTP %d (attempt %d/%d)", method, url, resp.status_code, attempt, attempts)
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < attempts:
                    self._sleep(min(int(retry_after), 60))
                    continue
            if attempt < attempts:
                self._sleep(min(2 ** attempt + random.random(), 30))
        assert last_exc is not None
        raise last_exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "PoliteClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _deny_all() -> RobotFileParser:
    rp = RobotFileParser()
    rp.parse(["User-agent: *", "Disallow: /"])
    return rp


def _looks_like_challenge(resp: httpx.Response) -> bool:
    if "text/html" not in resp.headers.get("content-type", ""):
        return False
    head = resp.text[:2000].lower()
    return "<title>just a moment" in head or "captcha" in head and "<form" in head
