"""
İşletim uçları: /health Redis denetimi ve dead-letter listesi (sıra + sınır).

Gerçek Redis gerekmez: Redis davranışı küçük bir sahte istemciyle taklit edilir.
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

import core.metrics as metrics_mod
import workers.tasks as tasks
from core.config import settings
from core.dedup import dedup
from core.metrics import metrics


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(settings, "public_read", False)
    from app.main import app
    return TestClient(app)


@pytest.fixture
def auth():
    return {"X-API-Key": settings.api_key}


# --------------------------------------------------------------------------
# /health
# --------------------------------------------------------------------------
class _PingClient:
    def __init__(self, ok: bool):
        self.ok = ok

    def ping(self):
        if not self.ok:
            raise ConnectionError("redis yok")
        return True

    def close(self):
        pass


def _fake_redis(monkeypatch, ok: bool):
    import redis
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *a, **k: _PingClient(ok)))


def test_health_redis_false_when_redis_down_even_if_broadcast_answers(client, monkeypatch):
    """Worker yayın ping'i boş liste döndürse de Redis'in kendisi kapalıysa redis=False."""
    from celery.app.control import Control
    monkeypatch.setattr(Control, "ping", lambda self, *a, **k: [])
    _fake_redis(monkeypatch, ok=False)
    data = client.get("/health").json()
    assert data["checks"]["redis"] is False
    assert data["status"] == "unhealthy"


def test_health_redis_true_when_redis_answers(client, monkeypatch):
    from celery.app.control import Control
    monkeypatch.setattr(Control, "ping", lambda self, *a, **k: (_ for _ in ()).throw(OSError("x")))
    _fake_redis(monkeypatch, ok=True)
    data = client.get("/health").json()
    assert data["checks"]["redis"] is True
    for key in ("status", "target", "dedup_backend", "checks"):
        assert key in data
    assert "database" in data["checks"]


def test_health_is_fast_when_redis_unreachable(client):
    """conftest REDIS_URL'i kapalı bir porta çevirir: denetim hızlı ve False olmalı."""
    start = time.monotonic()
    data = client.get("/health").json()
    assert data["checks"]["redis"] is False
    assert time.monotonic() - start < 5


# --------------------------------------------------------------------------
# Dead-letter: en yeniler önce, liste sınırlı
# --------------------------------------------------------------------------
class FakeListRedis:
    """Redis listesi komutlarının küçük bir taklidi (rpush/lpush/ltrim/lrange/llen)."""

    def __init__(self):
        self.lists: dict[str, list[str]] = {}

    def _l(self, key):
        return self.lists.setdefault(key, [])

    def rpush(self, key, *vals):
        self._l(key).extend(vals)
        return len(self._l(key))

    def lpush(self, key, *vals):
        for v in vals:
            self._l(key).insert(0, v)
        return len(self._l(key))

    @staticmethod
    def _range(lst, start, stop):
        n = len(lst)
        start = max(start + n if start < 0 else start, 0)
        stop = stop + n if stop < 0 else stop
        return lst[start:stop + 1]

    def lrange(self, key, start, stop):
        return list(self._range(self._l(key), start, stop))

    def ltrim(self, key, start, stop):
        self.lists[key] = self._range(self._l(key), start, stop)
        return True

    def llen(self, key):
        return len(self._l(key))


@pytest.fixture
def fake_redis(monkeypatch):
    fake = FakeListRedis()
    # Dead-letter yazımı/okuması hangi istemciyi kullanırsa kullansın aynı sahteyi görsün.
    monkeypatch.setattr(metrics, "_client", fake)
    monkeypatch.setattr(dedup, "_client", fake, raising=False)
    return fake


def _push(n: int):
    for i in range(n):
        tasks.push_dead_letter({"task": "workers.scrape_book", "args": [f"u{i}"], "error": "x"})


def test_dead_letter_returns_newest_first(client, auth, fake_redis):
    _push(5)
    data = client.get("/dead-letter?limit=2", headers=auth).json()
    assert [it["args"][0] for it in data["items"]] == ["u4", "u3"]
    assert data["count"] == 2


def test_dead_letter_list_is_bounded(fake_redis, monkeypatch):
    monkeypatch.setattr(metrics_mod, "DEAD_LETTER_MAX", 3)
    _push(10)
    assert metrics.snapshot()["dead_letter_depth"] == 3
    newest = [it["args"][0] for it in metrics.dead_letters(10)]
    assert newest == ["u9", "u8", "u7"]                # en yeniler tutulur


def test_dead_letter_falls_back_to_log_without_redis(monkeypatch, caplog):
    import logging
    monkeypatch.setattr(metrics, "_client", None)
    with caplog.at_level(logging.ERROR, logger="workers.tasks"):
        tasks.push_dead_letter({"task": "x", "args": ["u"], "error": "boom"})
    assert "[dead-letter]" in caplog.text
    assert metrics.dead_letters(10) == []
