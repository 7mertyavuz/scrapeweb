"""
Tarayıcı oturumunun toplam süre sınırı (core/browser.py).

``fetch_with_browser`` bağlanma dahil her adımıyla REQUEST_TIMEOUT x _DEADLINE_FACTOR
içinde dönmeli; süre dolunca iptal edilen bir iş parçacığı çağrısı (yavaş DNS) dönüşü
geciktirmemeli. Playwright sahte nesnelerle taklit edilir, ağa çıkılmaz.
"""
from __future__ import annotations

import asyncio
import time
import types

import pytest

import core.browser as browser_mod
import core.netguard as netguard
from core.config import settings

TIMEOUT = 0.6
DEADLINE = TIMEOUT * browser_mod._DEADLINE_FACTOR
SLACK = 0.5   # kapatma, olay döngüsü ve iş parçacığı payı


class _Fake:
    """``async_playwright`` yerine: bağlanma ve sayfa davranışı ayarlanabilir."""

    def __init__(self, connect_delay=0.0, chain=("http://site.example/",)):
        self.calls = []
        outer = self

        class Page:
            url = chain[-1]

            async def goto(self, url, **kw):
                prev = None
                for u in chain:
                    prev = types.SimpleNamespace(url=u, redirected_from=prev)
                return types.SimpleNamespace(request=prev)

            async def content(self):
                return "<html>icerik</html>"

        class Context:
            async def route(self, pattern, handler):
                pass

            async def new_page(self):
                return Page()

        class Browser:
            async def new_context(self, **kw):
                return Context()

            async def close(self):
                outer.calls.append("close")

        async def connect_over_cdp(url, timeout=None, **kw):
            await asyncio.sleep(min(connect_delay, timeout / 1000))
            outer.calls.append("connected")
            return Browser()

        self.chromium = types.SimpleNamespace(connect_over_cdp=connect_over_cdp)

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "request_timeout", TIMEOUT)
    monkeypatch.setattr(settings, "browser_egress_proxy", "http://egress:8899")
    monkeypatch.setattr(settings, "allow_private_networks", False)
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["93.184.216.34"])


def _timed(url="http://site.example/"):
    t0 = time.monotonic()
    result = browser_mod.fetch_with_browser(url)
    return result, time.monotonic() - t0


def test_normal_session_returns_content(monkeypatch):
    monkeypatch.setattr(browser_mod, "async_playwright", _Fake())
    result, _ = _timed()
    assert result == "<html>icerik</html>"


def test_slow_redirect_check_does_not_outlive_deadline(monkeypatch):
    """Yönlendirme zinciri denetimi (DNS) takılırsa iptal edilir ve beklenmez."""
    real = browser_mod.is_browser_request_allowed

    def slow_dns(url):
        time.sleep(3)
        return real(url)

    monkeypatch.setattr(browser_mod, "is_browser_request_allowed", slow_dns)
    fake = _Fake()
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    result, elapsed = _timed()
    assert result is None
    assert elapsed < DEADLINE + SLACK, f"{elapsed:.2f} sn"
    assert fake.calls[-1] == "close"


def test_slow_browserless_connect_counts_toward_deadline(monkeypatch):
    """Meşgul browserless'a bağlanma süresi oturum sınırının içindedir (ayrıca eklenmez)."""

    async def scrape_hanging(browser, url):
        await asyncio.sleep(3600)

    fake = _Fake(connect_delay=TIMEOUT * 0.95)   # bağlanma zaman aşımının hemen altında başarır
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    monkeypatch.setattr(browser_mod, "_scrape", scrape_hanging)
    result, elapsed = _timed()
    assert result is None
    assert elapsed < DEADLINE + TIMEOUT * 0.4, f"{elapsed:.2f} sn"
    assert fake.calls == ["connected", "close"]


def test_slow_target_check_is_bounded(monkeypatch):
    """Hedefin ilk denetimindeki yavaş DNS de oturum sınırının içindedir."""

    def slow_resolve(host, port):
        time.sleep(3)
        return ["93.184.216.34"]

    monkeypatch.setattr(netguard, "_resolve", slow_resolve)
    fake = _Fake()
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    result, elapsed = _timed()
    assert result is None
    assert elapsed < DEADLINE + SLACK, f"{elapsed:.2f} sn"
    assert fake.calls == [], "denetim bitmeden browserless'a bağlanılmamalı"
