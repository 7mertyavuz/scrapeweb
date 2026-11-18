"""
API endpoint testleri (kimlik doğrulama, PUBLIC_READ, CORS, girdi doğrulama dahil).
"""
from __future__ import annotations

import types

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import KNOWN_PUBLIC_API_KEYS, Settings, settings

OLD_PUBLIC_KEY = "BENIM_GIZLI_SIFREM_123"
READ_ENDPOINTS = ["/stats", "/books", "/dead-letter", "/status/some-task-id"]
UNAUTHORIZED = (401, 403)


@pytest.fixture
def client(monkeypatch):
    # Güvenlik ayarlarını her testte varsayılana çek (testler birbirini etkilemesin).
    monkeypatch.setattr(settings, "public_read", False)
    monkeypatch.setattr(settings, "allow_private_networks", False)
    from app.main import app
    return TestClient(app)


@pytest.fixture
def auth():
    assert settings.api_key_configured, "conftest bir test API anahtarı ayarlamalı"
    return {"X-API-Key": settings.api_key}


@pytest.fixture
def sent(monkeypatch):
    """Celery'ye gönderilen görevleri yakala (broker gerektirmez)."""
    from workers.celery_app import celery_app

    calls: list[tuple[str, dict]] = []

    def fake_send_task(name, kwargs=None, **_):
        calls.append((name, kwargs or {}))
        return types.SimpleNamespace(id=f"task-{len(calls)}")

    monkeypatch.setattr(celery_app, "send_task", fake_send_task)
    return calls


@pytest.fixture
def fake_async_result(monkeypatch):
    from workers.celery_app import celery_app

    class _Res:
        state = "PENDING"

        def ready(self):
            return False

    monkeypatch.setattr(celery_app, "AsyncResult", lambda task_id: _Res())


# --------------------------------------------------------------------------
# Açık uçlar
# --------------------------------------------------------------------------
def test_root_serves_frontend_or_api_info(client):
    res = client.get("/")
    assert res.status_code == 200
    # frontend dizini varsa HTML döner, yoksa JSON mesaj döner.
    assert "ScrapeHub" in res.text or res.json().get("message", "").startswith("ScrapeHub")


def test_static_css_served(client):
    res = client.get("/static/styles.css")
    assert res.status_code == 200
    assert "stylesheet" not in res.text.lower()  # CSS dosyası, HTML değil


def test_health_returns_json(client):
    res = client.get("/health")
    # Redis olmadan unhealthy (503) ya da tam ortamda healthy (200) olabilir.
    assert res.status_code in (200, 503)
    data = res.json()
    assert data["target"] == settings.base_url
    assert "checks" in data


def test_metrics_prometheus_format(client):
    res = client.get("/metrics")
    assert res.status_code == 200
    assert "scrapehub_" in res.text


# --------------------------------------------------------------------------
# Okuma uçları: varsayılan anahtar zorunlu, PUBLIC_READ=true ile eski davranış
# --------------------------------------------------------------------------
@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_require_key_by_default(client, fake_async_result, path):
    res = client.get(path)
    assert res.status_code in UNAUTHORIZED


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_reject_wrong_key(client, fake_async_result, path):
    res = client.get(path, headers={"X-API-Key": "yanlis-anahtar"})
    assert res.status_code in UNAUTHORIZED


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_read_endpoints_work_with_key(client, auth, fake_async_result, path):
    res = client.get(path, headers=auth)
    assert res.status_code == 200


@pytest.mark.parametrize("path", READ_ENDPOINTS)
def test_public_read_restores_anonymous_access(client, monkeypatch, fake_async_result, path):
    monkeypatch.setattr(settings, "public_read", True)
    res = client.get(path)
    assert res.status_code == 200


def test_stats_with_key(client, auth):
    res = client.get("/stats", headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert "books_in_db" in data
    assert "dedup_backend" in data


def test_stats_public_when_public_read(client, monkeypatch):
    monkeypatch.setattr(settings, "public_read", True)
    res = client.get("/stats")
    assert res.status_code == 200
    assert "books_in_db" in res.json()


def test_books_pagination(client, auth):
    res = client.get("/books?limit=10&offset=0", headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert data["limit"] == 10
    assert data["offset"] == 0
    assert "books" in data
    assert "total" in data


def test_books_validation_rejects_negative_offset(client, auth):
    res = client.get("/books?offset=-1", headers=auth)
    assert res.status_code == 422


def test_dead_letter_with_key(client, auth):
    res = client.get("/dead-letter", headers=auth)
    assert res.status_code == 200
    data = res.json()
    assert "items" in data
    assert "count" in data


def test_dead_letter_public_when_public_read(client, monkeypatch):
    monkeypatch.setattr(settings, "public_read", True)
    res = client.get("/dead-letter")
    assert res.status_code == 200
    assert "items" in res.json()


def test_public_read_parsed_from_env(monkeypatch):
    monkeypatch.delenv("PUBLIC_READ", raising=False)
    assert Settings().public_read is False
    monkeypatch.setenv("PUBLIC_READ", "true")
    assert Settings().public_read is True


# --------------------------------------------------------------------------
# Yazma uçları: her zaman anahtar zorunlu
# --------------------------------------------------------------------------
WRITE_CALLS = [
    ("/scrape", {"max_pages": 1, "chunk_size": 1}),
    ("/scrape-dynamic", {"max_pages": 1, "chunk_size": 1}),
    ("/scrape-one", {"url": "https://books.toscrape.com/catalogue/x.html"}),
]


@pytest.mark.parametrize("path,body", WRITE_CALLS)
def test_write_endpoints_require_api_key(client, sent, path, body):
    res = client.post(path, json=body)
    assert res.status_code in UNAUTHORIZED
    assert sent == []


@pytest.mark.parametrize("path,body", WRITE_CALLS)
def test_write_endpoints_stay_protected_with_public_read(client, sent, monkeypatch, path, body):
    monkeypatch.setattr(settings, "public_read", True)
    res = client.post(path, json=body)
    assert res.status_code in UNAUTHORIZED
    assert sent == []


@pytest.mark.parametrize("path,body", WRITE_CALLS)
def test_write_endpoints_accept_valid_key(client, auth, sent, path, body):
    res = client.post(path, json=body, headers=auth)
    assert res.status_code == 200
    assert res.json()["queued"] is True
    assert len(sent) == 1


def test_scrape_default_target_still_dispatched(client, auth, sent):
    res = client.post("/scrape", json={}, headers=auth)
    assert res.status_code == 200
    name, kwargs = sent[0]
    assert name == "workers.discover_catalog"
    assert kwargs["start_url"] is None and kwargs["max_pages"] == 5 and kwargs["chunk_size"] == 10


# --------------------------------------------------------------------------
# API_KEY: kullanılabilir varsayılan yok, eski sabit anahtar geçersiz
# --------------------------------------------------------------------------
def test_settings_has_no_default_api_key(monkeypatch):
    monkeypatch.delenv("API_KEY", raising=False)
    s = Settings()
    assert s.api_key == ""
    assert s.api_key_configured is False


def test_settings_rejects_known_public_key_even_if_configured(monkeypatch):
    assert OLD_PUBLIC_KEY in KNOWN_PUBLIC_API_KEYS
    monkeypatch.setenv("API_KEY", OLD_PUBLIC_KEY)
    assert Settings().api_key_configured is False


def test_old_hardcoded_key_no_longer_accepted(client, sent):
    for path, body in WRITE_CALLS:
        assert client.post(path, json=body, headers={"X-API-Key": OLD_PUBLIC_KEY}).status_code in UNAUTHORIZED
    assert client.get("/books", headers={"X-API-Key": OLD_PUBLIC_KEY}).status_code in UNAUTHORIZED
    assert sent == []


def test_old_key_rejected_even_when_explicitly_configured(client, monkeypatch, sent):
    monkeypatch.setattr(settings, "api_key", OLD_PUBLIC_KEY)
    res = client.post("/scrape", json={}, headers={"X-API-Key": OLD_PUBLIC_KEY})
    assert res.status_code in UNAUTHORIZED
    assert sent == []


@pytest.mark.parametrize("header", [None, "", " ", "herhangi-bir-sey"])
def test_unset_api_key_rejects_every_request(client, monkeypatch, sent, header):
    monkeypatch.setattr(settings, "api_key", "")
    headers = {} if header is None else {"X-API-Key": header}
    assert client.post("/scrape", json={}, headers=headers).status_code in UNAUTHORIZED
    assert client.get("/books", headers=headers).status_code in UNAUTHORIZED
    assert sent == []


def test_api_key_comparison_is_constant_time(client, auth, sent, monkeypatch):
    import app.main as main_mod

    seen = []
    real = main_mod.hmac.compare_digest

    def spy(a, b):
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(main_mod.hmac, "compare_digest", spy)
    assert client.post("/scrape", json={}, headers=auth).status_code == 200
    assert seen, "anahtar hmac.compare_digest ile karşılaştırılmalı"


# --------------------------------------------------------------------------
# Girdi sınırları
# --------------------------------------------------------------------------
@pytest.mark.parametrize("body", [
    {"max_pages": 10**9},
    {"max_pages": 0},
    {"max_pages": -1},
    {"chunk_size": -5},
    {"chunk_size": 0},
    {"chunk_size": 10**6},
])
@pytest.mark.parametrize("path", ["/scrape", "/scrape-dynamic"])
def test_scrape_rejects_out_of_range_limits(client, auth, sent, path, body):
    res = client.post(path, json=body, headers=auth)
    assert res.status_code == 422
    assert sent == []


def test_scrape_accepts_upper_limits(client, auth, sent):
    body = {"max_pages": settings.max_pages_limit, "chunk_size": 100}
    assert client.post("/scrape", json=body, headers=auth).status_code == 200


def test_rejects_overlong_url(client, auth, sent):
    url = "https://books.toscrape.com/" + "a" * 5000
    assert client.post("/scrape-one", json={"url": url}, headers=auth).status_code == 422
    assert sent == []


# --------------------------------------------------------------------------
# SSRF: API girdi katmanı
# --------------------------------------------------------------------------
BAD_URLS = [
    "http://127.0.0.1/",
    "http://127.0.0.1:6379/",
    "http://localhost:8000/books",
    "http://[::1]/",
    "http://[::ffff:127.0.0.1]/",
    "http://2130706433/",
    "http://0x7f.1/",
    "http://127.1/",
    "http://0.0.0.0/",
    "http://10.0.0.5/",
    "http://172.16.3.4/",
    "http://192.168.1.1/",
    "http://169.254.169.254/latest/meta-data/",
    "http://100.100.100.200/",
    "http://[fd00:ec2::254]/",
    "http://[fe80::1]/",
    "http://metadata.google.internal/",
    "http://redis:6379/",
    "http://api.localhost/",
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_FLUSHALL",
    "ftp://example.com/",
    "javascript:alert(1)",
    "dict://example.com:11211/",
    "http://user:pass@example.com/",
    "http://example.com\\@127.0.0.1/",
    "//example.com/",
    "not a url",
]


@pytest.mark.parametrize("url", BAD_URLS)
def test_scrape_one_rejects_unsafe_url(client, auth, sent, url):
    res = client.post("/scrape-one", json={"url": url}, headers=auth)
    assert res.status_code == 422
    assert sent == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "file:///etc/passwd", "gopher://x.example/", "http://169.254.169.254/"])
@pytest.mark.parametrize("path", ["/scrape", "/scrape-dynamic"])
def test_scrape_rejects_unsafe_start_url(client, auth, sent, path, url):
    res = client.post(path, json={"start_url": url}, headers=auth)
    assert res.status_code == 422
    assert sent == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8080/hook", "http://169.254.169.254/", "file:///tmp/x", "http://10.1.2.3/"])
def test_scrape_rejects_unsafe_webhook(client, auth, sent, url):
    res = client.post("/scrape", json={"webhook_url": url}, headers=auth)
    assert res.status_code == 422
    assert sent == []


def test_scrape_accepts_public_urls(client, auth, sent):
    body = {
        "start_url": "https://books.toscrape.com/catalogue/page-1.html",
        "webhook_url": "https://hooks.example.com/scrapehub",
    }
    res = client.post("/scrape", json=body, headers=auth)
    assert res.status_code == 200
    assert sent[0][1]["start_url"] == body["start_url"]
    assert sent[0][1]["webhook_url"] == body["webhook_url"]


def test_allow_private_networks_escape_hatch(client, auth, sent, monkeypatch):
    monkeypatch.setattr(settings, "allow_private_networks", True)
    ok = client.post("/scrape-one", json={"url": "http://127.0.0.1:8080/x"}, headers=auth)
    assert ok.status_code == 200
    # Şema denetimi kaçış kapısıyla kapanmaz.
    bad = client.post("/scrape-one", json={"url": "file:///etc/passwd"}, headers=auth)
    assert bad.status_code == 422


# --------------------------------------------------------------------------
# CORS
# --------------------------------------------------------------------------
def test_cors_closed_by_default(client):
    evil = "https://evil.example"
    res = client.get("/metrics", headers={"Origin": evil})
    assert "access-control-allow-origin" not in res.headers
    assert "access-control-allow-credentials" not in res.headers

    pre = client.options(
        "/scrape",
        headers={"Origin": evil, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "x-api-key"},
    )
    assert "access-control-allow-origin" not in pre.headers
    assert "access-control-allow-credentials" not in pre.headers


def test_cors_origins_default_empty(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == []
    monkeypatch.setenv("CORS_ORIGINS", " https://a.example , https://b.example/ ,")
    assert Settings().cors_origins == ["https://a.example", "https://b.example"]


def test_cors_allowlist_only_reflects_listed_origins():
    from app.main import install_cors

    mini = FastAPI()

    @mini.get("/ping")
    def ping():
        return {"ok": True}

    assert install_cors(mini, ["https://ok.example"]) is True
    c = TestClient(mini)

    good = c.get("/ping", headers={"Origin": "https://ok.example"})
    assert good.headers.get("access-control-allow-origin") == "https://ok.example"
    assert "access-control-allow-credentials" not in good.headers

    bad = c.get("/ping", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers

    assert install_cors(FastAPI(), []) is False
