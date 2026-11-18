"""
scrape_book / run_local: dedup ve artımlı (incremental) işaretlerinin ZAMANLAMASI.

Kural: bir URL ancak kitap DB'ye başarıyla yazıldıktan sonra "görüldü" sayılır;
artımlı modda içerik özeti de ancak o zaman saklanır. Aksi halde başarısız bir
deneme (fetch/parse/DB hatası) URL'yi kalıcı olarak "işlenmiş" gösterir ve
yeniden denemeler hiçbir şey çekmeden atlanır.

Ağa çıkılmaz: fetcher yamalanır, dedup her testte tazedir (memory backend).
"""
from __future__ import annotations

import pytest

import run_local
import workers.tasks as tasks
from core.config import settings
from core.dedup import Deduplicator
from storage.db import count_books

URL = "https://books.toscrape.com/catalogue/kitap_1/index.html"
BOOK_HTML = """
<html><body>
  <div class="product_main"><h1>Kitap 1</h1>
    <p class="price_color">£10.00</p>
    <p class="availability">In stock (3 available)</p>
    <p class="star-rating Three"></p>
  </div>
</body></html>
"""


class _Flaky:
    """İlk `fail` çağrıda None (fetch başarısız), sonra HTML döndürür."""

    def __init__(self, fail: int = 1, html: str = BOOK_HTML):
        self.fail, self.html, self.calls = fail, html, 0

    def __call__(self, url: str):
        self.calls += 1
        return None if self.calls <= self.fail else self.html


@pytest.fixture
def fresh(monkeypatch):
    d = Deduplicator()
    monkeypatch.setattr(tasks, "dedup", d)
    monkeypatch.setattr(settings, "incremental", False)
    return d


# --------------------------------------------------------------------------
# Gözlem 1: dedup işareti fetch'ten ÖNCE konuyordu
# --------------------------------------------------------------------------
def test_retry_after_fetch_failure_actually_fetches(fresh, monkeypatch):
    fake = _Flaky(fail=1)
    monkeypatch.setattr(tasks.fetcher, "get", fake)

    with pytest.raises(tasks.FetchError):          # 1. deneme: autoretry'ı tetikler
        tasks.scrape_book.run(URL)
    res = tasks.scrape_book.run(URL)               # yeniden deneme

    assert res["status"] == "ok", res
    assert fake.calls == 2, "yeniden deneme sayfayı gerçekten çekmeli"
    assert count_books() == 1


def test_failed_fetch_does_not_mark_url_seen(fresh, monkeypatch):
    monkeypatch.setattr(tasks.fetcher, "get", lambda url: None)
    with pytest.raises(tasks.FetchError):
        tasks.scrape_book.run(URL)
    assert fresh.seen(URL) is False


def test_parse_failure_does_not_mark_url_seen(fresh, monkeypatch):
    monkeypatch.setattr(tasks.fetcher, "get", lambda url: "<html>bos</html>")
    assert tasks.scrape_book.run(URL)["status"] == "parse_failed"
    assert fresh.seen(URL) is False


def test_success_marks_seen_and_next_call_skips(fresh, monkeypatch):
    fake = _Flaky(fail=0)
    monkeypatch.setattr(tasks.fetcher, "get", fake)
    assert tasks.scrape_book.run(URL)["status"] == "ok"
    assert tasks.scrape_book.run(URL)["status"] == "skipped_duplicate"
    assert fake.calls == 1


def test_force_bypasses_dedup(fresh, monkeypatch):
    fake = _Flaky(fail=0)
    monkeypatch.setattr(tasks.fetcher, "get", fake)
    tasks.scrape_book.run(URL)
    assert tasks.scrape_book.run(URL, force=True)["status"] == "ok"
    assert fake.calls == 2


def test_run_local_retries_failed_url(monkeypatch):
    monkeypatch.setattr(run_local, "dedup", Deduplicator())
    fake = _Flaky(fail=1)
    monkeypatch.setattr(run_local.fetcher, "get", fake)

    assert run_local.scrape_book_sync(URL) == "fetch_failed"
    assert run_local.scrape_book_sync(URL).startswith("ok")
    assert run_local.scrape_book_sync(URL) == "skip(dup)"


# --------------------------------------------------------------------------
# Gözlem 9: artımlı modda içerik özeti yazımdan ÖNCE saklanıyordu
# --------------------------------------------------------------------------
@pytest.fixture
def incremental(fresh, monkeypatch):
    monkeypatch.setattr(settings, "incremental", True)
    monkeypatch.setattr(tasks.fetcher, "get", lambda url: BOOK_HTML)
    return fresh


def test_incremental_parse_failure_is_retried_next_run(incremental, monkeypatch):
    real_parse = tasks.parse
    monkeypatch.setattr(tasks, "parse", lambda html, url: None)
    assert tasks.scrape_book.run(URL)["status"] == "parse_failed"

    monkeypatch.setattr(tasks, "parse", real_parse)   # ör. ayrıştırıcı düzeltildi
    assert tasks.scrape_book.run(URL)["status"] == "updated"
    assert count_books() == 1


def test_incremental_db_failure_is_retried_next_run(incremental, monkeypatch):
    real_upsert = tasks.upsert_book

    def broken(book):
        raise RuntimeError("db down")

    monkeypatch.setattr(tasks, "upsert_book", broken)
    with pytest.raises(RuntimeError):
        tasks.scrape_book.run(URL)

    monkeypatch.setattr(tasks, "upsert_book", real_upsert)
    assert tasks.scrape_book.run(URL)["status"] == "updated"
    assert count_books() == 1


def test_incremental_unchanged_after_success_is_skipped(incremental):
    assert tasks.scrape_book.run(URL)["status"] == "updated"
    assert tasks.scrape_book.run(URL)["status"] == "skipped_unchanged"


# --------------------------------------------------------------------------
# Dead-letter: kwargs ile gönderilen görevlerde URL kayboluyordu
# --------------------------------------------------------------------------
def test_dead_letter_keeps_kwargs(monkeypatch):
    """/scrape-one görevi kwargs ile gönderir; dead-letter kaydı URL'yi taşımalı."""
    captured: list[dict] = []
    monkeypatch.setattr(tasks, "push_dead_letter", captured.append)
    tasks.scrape_book.on_failure(
        tasks.FetchError("x"), "tid", (), {"url": URL, "force": False, "use_ai": False}, None
    )
    assert captured[0]["args"] == []
    assert captured[0]["kwargs"]["url"] == URL
