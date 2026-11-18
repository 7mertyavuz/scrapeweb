"""
discover_catalog batch/chunk testi.

Celery'yi EAGER moda alıp (broker/worker gerektirmez) fetcher'ı sahte HTML döndürecek
şekilde yamalıyoruz. Böylece ağ'a çıkmadan:
  - chunked modun beklenen sayıda chunk ürettiğini,
  - tüm kitapların pipeline'dan geçip DB'ye yazıldığını,
  - chunk_size<=1 iken geriye uyumlu per_url moduna düştüğünü
doğruluyoruz.
"""
from __future__ import annotations

import pytest

from core.dedup import Deduplicator
import core.dedup as dedup_mod
import workers.tasks as tasks
from workers.celery_app import celery_app
from storage.db import count_books


CATALOG = """
<html><body>
  <article class="product_pod"><h3><a href="book-1.html">B1</a></h3></article>
  <article class="product_pod"><h3><a href="book-2.html">B2</a></h3></article>
  <article class="product_pod"><h3><a href="book-3.html">B3</a></h3></article>
  <article class="product_pod"><h3><a href="book-4.html">B4</a></h3></article>
  <article class="product_pod"><h3><a href="book-5.html">B5</a></h3></article>
</body></html>
"""

def _book_html(n: str) -> str:
    return f"""
    <html><body>
      <div class="product_main"><h1>Kitap {n}</h1>
        <p class="price_color">£{n}.00</p>
        <p class="availability">In stock (3 available)</p>
        <p class="star-rating Three"></p>
      </div>
    </body></html>
    """


def _fake_get(url: str):
    if "page-" in url:
        return CATALOG
    # book-N.html -> N
    import re
    m = re.search(r"book-(\d+)", url)
    return _book_html(m.group(1)) if m else None


@pytest.fixture
def eager(monkeypatch):
    # Celery ayarları elle kurulup geri alınır: pytest 9.2'de monkeypatch.setattr
    # eski değeri nesnenin __dict__'inden okur; Celery'nin Settings nesnesinde
    # bulamayınca teardown'da delattr'a düşüp AttributeError verir.
    conf = celery_app.conf
    old = {k: conf[k] for k in ("task_always_eager", "task_eager_propagates")}
    conf.update(task_always_eager=True, task_eager_propagates=True)
    # fetcher'ı her iki referans noktasında da yamala.
    monkeypatch.setattr(tasks.fetcher, "get", _fake_get)
    # Her test taze dedup kullansın (global'i kirletme).
    monkeypatch.setattr(dedup_mod, "dedup", Deduplicator())
    monkeypatch.setattr(tasks, "dedup", dedup_mod.dedup)
    monkeypatch.setattr(tasks.settings, "incremental", False)
    try:
        yield
    finally:
        conf.update(old)


def test_chunked_dispatch_processes_all_books(eager):
    res = tasks.discover_catalog.apply(args=[None, 1, 2]).get()
    assert res["mode"] == "chunked"
    assert res["books_dispatched"] == 5
    assert res["chunks"] == 3            # ceil(5/2)
    assert res["chunk_size"] == 2
    assert count_books() == 5            # hepsi DB'ye yazıldı


def test_backward_compatible_per_url(eager):
    res = tasks.discover_catalog.apply(args=[None, 1, 1]).get()
    assert res["mode"] == "per_url"      # chunk_size<=1 -> eski davranış
    assert res["books_dispatched"] == 5
    assert count_books() == 5


# --------------------------------------------------------------------------
# Chunk modu: bir URL'nin hatası chunk'ın kalanını düşürmemeli; webhook sayımı
# --------------------------------------------------------------------------
def _failing_for(n: str):
    def get(url: str):
        return None if f"book-{n}." in url else _fake_get(url)
    return get


@pytest.fixture
def requeued(monkeypatch):
    """Chunk içinde başarısız olan URL'nin tekil görev olarak kuyruğa atılışını yakala."""
    calls: list[tuple] = []
    monkeypatch.setattr(tasks.scrape_book, "delay", lambda *a, **k: calls.append((a, k)))
    return calls


@pytest.fixture
def webhook_sent(monkeypatch):
    sent: list[dict] = []

    def fake_request(method, url, json=None, **kwargs):
        sent.append({"method": method, "url": url, "json": json})

    monkeypatch.setattr(tasks, "guarded_request", fake_request)
    return sent


def test_chunk_failure_does_not_drop_rest_of_chunk(eager, monkeypatch, requeued):
    monkeypatch.setattr(tasks.fetcher, "get", _failing_for("2"))
    res = tasks.discover_catalog.apply(args=[None, 1, 2]).get()

    assert res["mode"] == "chunked"
    assert count_books() == 4                     # 1,3,4,5: aynı chunk'taki 1 dahil
    failed = "https://books.toscrape.com/catalogue/book-2.html"
    # Başarısız URL tekil scrape_book görevi olarak yeniden kuyruğa girer
    # (autoretry + dead-letter oradan gelir).
    assert requeued == [((failed,), {"force": False, "use_ai": False})]


def test_chord_webhook_counts_books_not_chunks(eager, webhook_sent):
    res = tasks.discover_catalog.apply(
        args=[None, 1, 2], kwargs={"webhook_url": "https://hooks.example/done"}
    ).get()
    assert res["webhook_attached"] is True
    assert len(webhook_sent) == 1
    body = webhook_sent[0]["json"]
    assert body["event"] == "scraping_completed"
    assert body["total_processed"] == 5           # 3 chunk değil, 5 kitap
    assert body["successful_inserts"] == 5
    assert body["meta"]["books_dispatched"] == 5


def test_chord_webhook_fires_when_a_url_fails(eager, monkeypatch, requeued, webhook_sent):
    monkeypatch.setattr(tasks.fetcher, "get", _failing_for("2"))
    tasks.discover_catalog.apply(
        args=[None, 1, 2], kwargs={"webhook_url": "https://hooks.example/done"}
    ).get()
    assert len(webhook_sent) == 1, "chord geri çağrısı tetiklenmeli"
    body = webhook_sent[0]["json"]
    assert body["total_processed"] == 5
    assert body["successful_inserts"] == 4
    assert body["requeued"] == 1


def test_webhook_callback_flattens_chunk_results(webhook_sent):
    nested = [
        [{"status": "ok"}, {"status": "ok"}],
        [{"status": "updated"}, {"status": "skipped_duplicate"}],
        [{"status": "requeued"}],
    ]
    tasks.webhook_callback.run(nested, "https://hooks.example/done", {"m": 1})
    body = webhook_sent[0]["json"]
    assert body["total_processed"] == 5
    assert body["successful_inserts"] == 3
    assert body["requeued"] == 1
    assert body["meta"] == {"m": 1}


def test_webhook_callback_accepts_flat_results(webhook_sent):
    tasks.webhook_callback.run([{"status": "ok"}, {"status": "parse_failed"}], "https://h.example/", {})
    body = webhook_sent[0]["json"]
    assert (body["total_processed"], body["successful_inserts"]) == (2, 1)
