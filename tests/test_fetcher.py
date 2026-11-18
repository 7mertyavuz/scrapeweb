"""
Fetcher davranışı (ağa çıkmadan): robots.txt alan adı başına, 5xx geri çekilmesi,
son denemeden sonra boşuna uyumama.

`core.fetcher.guarded_request` sahte yanıtlarla, `time.sleep` kayıt tutan bir
sahteyle değiştirilir; settings.base_url ulaşılamaz bir yerel adrese çekilir ki
eski (urllib tabanlı) robots okuması da dışarı çıkamasın.
"""
from __future__ import annotations

import types

import pytest
from curl_cffi import requests as curl_requests

import core.fetcher as fetcher_mod
from core.config import settings
from core.fetcher import Fetcher, allowed_by_robots
from core.netguard import UnsafeURLError

DELAY = 1.0


def _resp(status: int, text: str = ""):
    return types.SimpleNamespace(status_code=status, text=text, headers={})


class FakeNet:
    """URL -> yanıt listesi (sırayla tüketilir; son öğe tekrar eder) ya da istisna."""

    def __init__(self, routes: dict):
        self.routes = {k: list(v) if isinstance(v, list) else [v] for k, v in routes.items()}
        self.calls: list[str] = []

    def __call__(self, method, url, **kwargs):
        self.calls.append(url)
        seq = self.routes.get(url)
        if seq is None:
            return _resp(404)
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def env(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(fetcher_mod.time, "sleep", sleeps.append)
    monkeypatch.setattr(settings, "base_url", "http://127.0.0.1:9/")   # dışarı çıkılamaz
    monkeypatch.setattr(settings, "request_delay_sec", DELAY)
    monkeypatch.setattr(settings, "domain_delays", {})
    monkeypatch.setattr(settings, "max_retries", 3)
    monkeypatch.setattr(settings, "proxy_tier_1", "")
    monkeypatch.setattr(settings, "proxy_tier_2", "")
    monkeypatch.setattr(settings, "respect_robots", False)
    fetcher_mod._robots_cache.clear()
    yield sleeps
    fetcher_mod._robots_cache.clear()


def _install(monkeypatch, routes: dict) -> FakeNet:
    net = FakeNet(routes)
    monkeypatch.setattr(fetcher_mod, "guarded_request", net)
    return net


def _backoffs(sleeps: list[float]) -> list[float]:
    """Nezaket gecikmesi (DELAY) dışındaki uykular = geri çekilme beklemeleri."""
    return [s for s in sleeps if s != DELAY]


# --------------------------------------------------------------------------
# Yeniden deneme / bekleme
# --------------------------------------------------------------------------
def test_5xx_is_retried_with_backoff(env, monkeypatch):
    url = "https://shop.example/p"
    net = _install(monkeypatch, {url: [_resp(500), _resp(502), _resp(200, "<html>ok</html>")]})
    assert Fetcher().get(url) == "<html>ok</html>"
    assert len(net.calls) == 3
    assert _backoffs(env) == [DELAY * 2, DELAY * 4]


def test_persistent_5xx_gives_up_without_trailing_sleep(env, monkeypatch):
    url = "https://shop.example/p"
    _install(monkeypatch, {url: _resp(500)})
    assert Fetcher().get(url) is None
    assert len(_backoffs(env)) == settings.max_retries - 1


def test_waf_block_does_not_sleep_after_last_attempt(env, monkeypatch):
    url = "https://shop.example/p"
    _install(monkeypatch, {url: _resp(403)})
    assert Fetcher().get(url) is None
    assert _backoffs(env) == [DELAY * 2, DELAY * 4]   # 3. denemeden sonra bekleme yok


def test_network_error_does_not_sleep_after_last_attempt(env, monkeypatch):
    url = "https://shop.example/p"
    _install(monkeypatch, {url: curl_requests.RequestsError("baglanti koptu")})
    assert Fetcher().get(url) is None
    assert _backoffs(env) == [DELAY * 2, DELAY * 4]


def test_plain_4xx_is_not_retried(env, monkeypatch):
    url = "https://shop.example/p"
    net = _install(monkeypatch, {url: _resp(404)})
    assert Fetcher().get(url) is None
    assert len(net.calls) == 1


# --------------------------------------------------------------------------
# robots.txt: alan adı başına ve netguard üzerinden
# --------------------------------------------------------------------------
ROBOTS_A = "User-agent: *\nDisallow: /private\n"


def test_robots_rules_are_per_host(env, monkeypatch):
    monkeypatch.setattr(settings, "respect_robots", True)
    net = _install(monkeypatch, {
        "https://a.example/robots.txt": _resp(200, ROBOTS_A),
        "https://b.example/robots.txt": _resp(404),
    })
    assert allowed_by_robots("https://a.example/private/x") is False
    assert allowed_by_robots("https://a.example/public/x") is True
    # a.example'ın kuralları b.example'a uygulanmamalı.
    assert allowed_by_robots("https://b.example/private/x") is True
    # robots.txt korumalı istek yolundan (netguard) çekildi ve önbelleğe alındı.
    allowed_by_robots("https://a.example/other")
    assert net.calls.count("https://a.example/robots.txt") == 1
    assert net.calls.count("https://b.example/robots.txt") == 1


def test_robots_401_403_disallow_everything(env, monkeypatch):
    monkeypatch.setattr(settings, "respect_robots", True)
    _install(monkeypatch, {"https://c.example/robots.txt": _resp(403)})
    assert allowed_by_robots("https://c.example/anything") is False


def test_fetcher_skips_disallowed_url_without_requesting_it(env, monkeypatch):
    monkeypatch.setattr(settings, "respect_robots", True)
    net = _install(monkeypatch, {"https://a.example/robots.txt": _resp(200, ROBOTS_A)})
    assert Fetcher().get("https://a.example/private/x") is None
    assert net.calls == ["https://a.example/robots.txt"]


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(fetcher_mod, "_now", c)
    return c


def test_unreachable_robots_does_not_block_forever(env, monkeypatch, clock):
    """Geçici robots.txt hatası her şeyi kalıcı olarak belirlememeli: süre dolunca yeniden denenir."""
    monkeypatch.setattr(settings, "respect_robots", True)
    robots = "https://d.example/robots.txt"
    _install(monkeypatch, {robots: [curl_requests.RequestsError("zaman aşımı"), _resp(200, ROBOTS_A)]})
    assert allowed_by_robots("https://d.example/private/x") is True     # okunamadı -> izin
    clock.t += fetcher_mod.ROBOTS_ERROR_TTL
    assert allowed_by_robots("https://d.example/private/x") is False    # sonra okundu -> kural


@pytest.mark.parametrize("failure", [
    _resp(503), _resp(500),
    curl_requests.RequestsError("zaman aşımı"),
], ids=["503", "500", "timeout"])
def test_unreadable_robots_is_not_refetched_before_every_page(env, monkeypatch, clock, failure):
    """Gözlenen hata: robots 503 iken 5 sayfa -> 5 robots isteği (zaman aşımında her sayfaya +timeout)."""
    monkeypatch.setattr(settings, "respect_robots", True)
    robots = "https://e.example/robots.txt"
    pages = [f"https://e.example/p{i}" for i in range(5)]
    net = _install(monkeypatch, {robots: failure, **{p: _resp(200, "ok") for p in pages}})
    for p in pages:
        assert Fetcher().get(p) == "ok"                  # okunamadı -> izinli sayılır
        clock.t += 1
    assert net.calls.count(robots) == 1
    assert [c for c in net.calls if c != robots] == pages


def test_robots_rules_are_refreshed_after_ttl(env, monkeypatch, clock):
    """Okunan kurallar worker süreci yaşadıkça değil, ROBOTS_TTL boyunca geçerli."""
    monkeypatch.setattr(settings, "respect_robots", True)
    robots = "https://f.example/robots.txt"
    net = _install(monkeypatch, {robots: [_resp(200, ROBOTS_A), _resp(200, "User-agent: *\nDisallow:\n")]})
    assert allowed_by_robots("https://f.example/private/x") is False
    clock.t += fetcher_mod.ROBOTS_TTL - 1
    assert allowed_by_robots("https://f.example/private/x") is False    # hâlâ önbellekte
    assert net.calls.count(robots) == 1
    clock.t += 1
    assert allowed_by_robots("https://f.example/private/x") is True     # yenilendi
    assert net.calls.count(robots) == 2


def test_refresh_failure_keeps_last_known_rules(env, monkeypatch, clock):
    """Yenileme geçici olarak başarısızsa son okunan kurallar korunur (birden 'her şey serbest' olmaz)."""
    monkeypatch.setattr(settings, "respect_robots", True)
    robots = "https://g.example/robots.txt"
    net = _install(monkeypatch, {robots: [_resp(200, ROBOTS_A), _resp(503), _resp(200, "User-agent: *\nDisallow:\n")]})
    assert allowed_by_robots("https://g.example/private/x") is False
    clock.t += fetcher_mod.ROBOTS_TTL
    assert allowed_by_robots("https://g.example/private/x") is False    # 503: eski kural sürer
    assert allowed_by_robots("https://g.example/private/y") is False    # ve hemen yeniden istenmez
    assert net.calls.count(robots) == 2
    clock.t += fetcher_mod.ROBOTS_ERROR_TTL
    assert allowed_by_robots("https://g.example/private/x") is True
    assert net.calls.count(robots) == 3


def test_robots_cache_is_bounded(env, monkeypatch, clock):
    monkeypatch.setattr(settings, "respect_robots", True)
    _install(monkeypatch, {})
    for i in range(fetcher_mod.ROBOTS_CACHE_SIZE + 50):
        allowed_by_robots(f"https://h{i}.example/x")
    assert len(fetcher_mod._robots_cache) <= fetcher_mod.ROBOTS_CACHE_SIZE


def test_robots_unsafe_host_makes_no_request(env, monkeypatch):
    """İç adres: robots.txt de sayfa da netguard'da durur; hiçbir istek gönderilmez."""
    monkeypatch.setattr(settings, "respect_robots", True)
    sent: list[str] = []

    def guarded(method, url, **kwargs):
        sent.append(url)
        raise UnsafeURLError("iç adres")

    monkeypatch.setattr(fetcher_mod, "guarded_request", guarded)
    assert Fetcher().get("http://10.0.0.5/x") is None
    assert all(u.startswith("http://10.0.0.5") for u in sent)
