"""
Worker görevleri = pipeline'ın çalışan kalbi.
Webhook (Olay Güdümlü) ve Otonom (Playwright/AI) mantığı eklendi.
"""
from __future__ import annotations
import json
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from celery import shared_task, Task, chord, group

from core.config import settings
from core.dedup import dedup
from core.fetcher import fetcher
from core.logging import get_logger
from core.metrics import metrics
from core.netguard import UnsafeURLError, guarded_request
from parser.extract import parse
from storage.db import init_db, upsert_book
from core.browser import fetch_with_browser
from parser.ai_extract import parse_with_llama

logger = get_logger(__name__)
init_db()

class FetchError(Exception): pass

def push_dead_letter(payload: dict) -> None:
    """Kalıcı başarısız görevi dead-letter listesine yaz; Redis yoksa loga düş."""
    if not metrics.push_dead_letter(payload):
        logger.error("[dead-letter] %s", json.dumps(payload, ensure_ascii=False))

class DeadLetterTask(Task):
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        push_dead_letter({
            "task": self.name,
            "task_id": task_id,
            "args": list(args),
            # /scrape-one görevi URL'yi kwargs ile gönderir; onu da sakla.
            "kwargs": dict(kwargs or {}),
            "error": repr(exc),
        })

def _flatten_results(results) -> list:
    """Chord sonuçları chunk başına bir liste gelir; URL başına düz listeye çevir."""
    flat: list = []
    for r in results or []:
        if isinstance(r, (list, tuple)):
            flat.extend(r)
        else:
            flat.append(r)
    return flat

# --- KONU 4: Event-Driven Webhook Görevi ---
@shared_task(name="workers.webhook_callback")
def webhook_callback(results, webhook_url: str, meta: dict):
    """Tüm URL'ler kazındıktan SONRA otomatik çalışır ve müşteriye POST atar."""
    results = _flatten_results(results)
    statuses = [r.get("status") for r in results if isinstance(r, dict)]
    payload = {
        "event": "scraping_completed",
        "meta": meta,
        "total_processed": len(results),
        "successful_inserts": sum(1 for s in statuses if s in ("ok", "updated")),
        # Chunk içinde başarısız olup tekil görev olarak yeniden kuyruğa atılanlar
        # (sonuçları bu webhook'tan sonra gelir).
        "requeued": statuses.count("requeued"),
    }
    try:
        logger.info(f"Webhook tetikleniyor -> {webhook_url}")
        # SSRF korumalı: iç/özel adreslere gönderilmez, yönlendirme izlenmez.
        guarded_request("POST", webhook_url, json=payload, timeout=5, max_redirects=0)
    except UnsafeURLError as e:
        logger.warning(f"Webhook engellendi (iç/özel ağ ya da geçersiz URL): {e}")
    except Exception as e:
        logger.error(f"Webhook gönderilemedi: {e}")

@shared_task(
    bind=True, base=DeadLetterTask, name="workers.scrape_book",
    autoretry_for=(FetchError,), retry_backoff=True, retry_jitter=True,
    max_retries=None, retry_kwargs={"max_retries": settings.max_retries},
)
def scrape_book(self, url: str, force: bool = False, use_ai: bool = False) -> dict:
    # URL dedup: yalnız KONTROL. URL, kitap DB'ye yazıldıktan sonra işaretlenir;
    # böylece başarısız deneme (ve onun autoretry'ı) URL'yi "görüldü" bırakmaz.
    # Incremental modda içerik hash'i için mecburen fetch ederiz.
    if not force and not settings.incremental and dedup.seen(url):
        return {"url": url, "status": "skipped_duplicate"}

    # --- KONU 1, 2, 3: Karar Ağacı ---
    if use_ai:
        # Dinamik (Playwright) Fetch + Llama Parse
        html = fetch_with_browser(url)
        if not html:
            metrics.inc("failed_fetch")
            raise FetchError(f"Browser fetch failed: {url}")
        
        book = parse_with_llama(html, url)
    else:
        # Standart (curl_cffi) Fetch + CSS Parse
        html = fetcher.get(url)
        if not html:
            metrics.inc("failed_fetch")
            raise FetchError(f"Fetch failed: {url}")
            
        book = parse(html, url)

    # Incremental (İçerik) Kontrolü (Html çektikten sonra). Hash burada yalnız
    # karşılaştırılır; ancak yazım başarılı olunca saklanır.
    if settings.incremental and not force:
        if not dedup.content_changed(url, html, remember=False):
            dedup.mark(url)
            return {"url": url, "status": "skipped_unchanged"}

    if not book:
        return {"url": url, "status": "parse_failed"}

    upsert_book(book)
    if settings.incremental:
        dedup.remember_content(url, html)
    dedup.mark(url)
    metrics.inc("pages_processed")
    return {"url": url, "status": "updated" if settings.incremental else "ok", "title": book.title, "price": book.price}

@shared_task(name="workers.scrape_chunk")
def scrape_chunk(urls: list[str], use_ai: bool = False) -> list[dict]:
    """Bir chunk'taki URL'leri sırayla işler (Celery'nin .chunks() karşılığı).

    Fark: bir URL'nin hatası chunk'ın kalanını düşürmez. Doğrudan (aynı süreçte)
    çağrılan scrape_book'ta autoretry çalışmadığından, hata alan URL tekil bir
    scrape_book görevi olarak yeniden kuyruğa atılır; yeniden deneme ve
    dead-letter oradan gelir. Chunk görevi hiç hata vermediği için chord geri
    çağrısı (webhook) her durumda tetiklenir.
    """
    results: list[dict] = []
    for url in urls:
        try:
            results.append(scrape_book(url, False, use_ai))
        except Exception as exc:
            logger.warning("[chunk] %s başarısız, tekil görev olarak yeniden kuyrukta: %r", url, exc)
            scrape_book.delay(url, force=False, use_ai=use_ai)
            results.append({"url": url, "status": "requeued", "error": repr(exc)})
    return results

@shared_task(bind=True, name="workers.discover_catalog")
def discover_catalog(
    self,
    start_url: str | None = None,
    max_pages: int = 5,
    chunk_size: int = 10,
    webhook_url: str | None = None,
    use_ai: bool = False
) -> dict:
    base = settings.base_url
    page_url = start_url or urljoin(base, "catalogue/page-1.html")
    pages_done = 0
    book_urls: list[str] = []

    while page_url and pages_done < max_pages:
        html = fetcher.get(page_url)
        if not html: break
        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("article.product_pod h3 a"):
            href = a.get("href", "")
            book_urls.append(urljoin(page_url, href))

        nxt = soup.select_one("li.next a")
        page_url = urljoin(page_url, nxt.get("href")) if nxt else None
        pages_done += 1

    if chunk_size and chunk_size > 1 and book_urls:
        task_group = group(
            scrape_chunk.s(book_urls[i:i + chunk_size], use_ai)
            for i in range(0, len(book_urls), chunk_size)
        )

        # Webhook varsa Celery Chord ile bağla, yoksa normal asenkron çalıştır
        if webhook_url:
            meta = {"pages_scanned": pages_done, "books_dispatched": len(book_urls), "ai_used": use_ai}
            callback = webhook_callback.s(webhook_url, meta)
            group_result = chord(task_group)(callback)
        else:
            group_result = task_group.apply_async()
            
        num_chunks = (len(book_urls) + chunk_size - 1) // chunk_size
        return {
            "mode": "chunked",
            "webhook_attached": bool(webhook_url),
            "books_dispatched": len(book_urls),
            "chunks": num_chunks,
            "chunk_size": chunk_size,
            "group_id": getattr(group_result, "id", None),
        }

    for u in book_urls:
        scrape_book.delay(u, force=False, use_ai=use_ai)
    
    return {
        "pages_scanned": pages_done,
        "books_dispatched": len(book_urls),
        "mode": "per_url",
    }
