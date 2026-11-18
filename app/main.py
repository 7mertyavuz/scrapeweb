"""
Katman 1: API Gateway (FastAPI).
OpenTelemetry, yeni AI uç noktaları ve web arayüzü ile güncellendi.
"""
from __future__ import annotations
import hmac
from contextlib import suppress
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from core.config import settings
from core.dedup import dedup
from core.logging import get_logger
from core.metrics import metrics
from core.netguard import MAX_URL_LENGTH, UnsafeURLError, check_api_url
from storage.db import count_books, get_books, init_db, ping_db

# Sisteme doğrudan ana Celery uygulamamızı tanıtıyoruz
from workers.celery_app import celery_app

from core.tracing import setup_tracing
setup_tracing("scrapehub-api")
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

logger = get_logger(__name__)

app = FastAPI(title="ScrapeHub AI", version="2.1", description="Otonom Web Scraping Pipeline")

def install_cors(target: FastAPI, origins: list[str]) -> bool:
    """CORS'u yalnız açıkça izin verilen origin'ler için aç.

    Ön yüz API ile aynı origin'den servis edildiği için varsayılanda (liste boş)
    CORS middleware hiç eklenmez. Kimlik doğrulama çerez değil X-API-Key başlığı
    olduğundan kimlik bilgisi (credentials) hiçbir zaman izinli değildir.
    """
    if not origins:
        return False
    if "*" in origins:
        logger.warning("CORS_ORIGINS='*': her origin API'yi çağırabilir (kimlik bilgisi yine kapalı).")
    target.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )
    return True


install_cors(app, settings.cors_origins)

FastAPIInstrumentor.instrument_app(app)

init_db()

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

if not settings.api_key_configured:
    logger.warning(
        "API_KEY ayarlı değil (ya da herkesçe bilinen eski varsayılan): "
        "anahtar gerektiren uçlar tüm istekleri 401 ile reddedecek."
    )

# Frontend dosyalarını servis et
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
if FRONTEND_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="Geçersiz yetki", headers={"WWW-Authenticate": "ApiKey"})


def verify_api_key(api_key: Optional[str] = Security(api_key_header)) -> str:
    # Anahtar ayarlı değilse hiçbir istek kimlik doğrulayamaz (boş == boş eşleşmesi dahil).
    if not settings.api_key_configured or not api_key:
        raise _unauthorized()
    if not hmac.compare_digest(api_key.encode("utf-8"), settings.api_key.encode("utf-8")):
        raise _unauthorized()
    return api_key


def verify_read_access(api_key: Optional[str] = Security(api_key_header)) -> Optional[str]:
    """Okuma uçları: PUBLIC_READ=true ise anahtarsız, değilse anahtar zorunlu."""
    if settings.public_read:
        return api_key
    return verify_api_key(api_key)


def _validate_target_url(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return check_api_url(value)
    except UnsafeURLError as exc:
        raise ValueError(str(exc)) from exc


class ScrapeRequest(BaseModel):
    start_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)
    max_pages: int = Field(default=5, ge=1, le=settings.max_pages_limit)
    chunk_size: int = Field(default=10, ge=1, le=100)
    webhook_url: str | None = Field(default=None, max_length=MAX_URL_LENGTH)

    @field_validator("start_url", "webhook_url")
    @classmethod
    def _check_urls(cls, value: Optional[str]) -> Optional[str]:
        return _validate_target_url(value)


class ScrapeOneRequest(BaseModel):
    url: str = Field(max_length=MAX_URL_LENGTH)
    force: bool = False

    @field_validator("url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        return _validate_target_url(value)


@app.get("/")
def root():
    """Ana sayfa olarak frontend'i döndür."""
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "ScrapeHub API çalışıyor", "docs": "/docs"}


@app.post("/scrape")
def start_scrape(req: ScrapeRequest, api_key: str = Depends(verify_api_key)) -> dict:
    task = celery_app.send_task(
        "workers.discover_catalog",
        kwargs={
            "start_url": req.start_url,
            "max_pages": req.max_pages,
            "chunk_size": req.chunk_size,
            "webhook_url": req.webhook_url,
            "use_ai": False,
        },
    )
    return {"task_id": task.id, "queued": True, "type": "standard"}


@app.post("/scrape-dynamic")
def start_dynamic_scrape(req: ScrapeRequest, api_key: str = Depends(verify_api_key)) -> dict:
    task = celery_app.send_task(
        "workers.discover_catalog",
        kwargs={
            "start_url": req.start_url,
            "max_pages": req.max_pages,
            "chunk_size": req.chunk_size,
            "webhook_url": req.webhook_url,
            "use_ai": True,
        },
    )
    return {"task_id": task.id, "queued": True, "type": "ai_agentic"}


@app.post("/scrape-one")
def scrape_single(req: ScrapeOneRequest, api_key: str = Depends(verify_api_key)) -> dict:
    task = celery_app.send_task(
        "workers.scrape_book",
        kwargs={"url": req.url, "force": req.force, "use_ai": False},
    )
    return {"task_id": task.id, "queued": True, "url": req.url}


@app.get("/status/{task_id}")
def status(task_id: str, _auth: Optional[str] = Depends(verify_read_access)) -> dict:
    res = celery_app.AsyncResult(task_id)
    info: dict = {"task_id": task_id, "state": res.state}
    if res.ready():
        with suppress(Exception):
            info["result"] = res.get(propagate=False)
    return info


@app.get("/stats")
def stats(_auth: Optional[str] = Depends(verify_read_access)) -> dict:
    return {"books_in_db": count_books(), "dedup_backend": dedup.backend}


@app.get("/books")
def list_books(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _auth: Optional[str] = Depends(verify_read_access),
) -> dict:
    """Veritabanındaki kitapları sayfalayarak döndür."""
    books, total = get_books(limit=limit, offset=offset)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "books": [b.model_dump() for b in books],
    }


@app.get("/dead-letter")
def list_dead_letter(
    limit: int = Query(100, ge=1, le=1000),
    _auth: Optional[str] = Depends(verify_read_access),
) -> dict:
    """Son başarısız görevleri (dead-letter kuyruğu) döndür."""
    items: list[dict] = []
    client = getattr(dedup, "_client", None)
    if client:
        try:
            raw_items = client.lrange("scrapehub:dead_letter", 0, limit - 1)
            import json
            for raw in raw_items:
                with suppress(Exception):
                    items.append(json.loads(raw))
        except Exception as exc:
            logger.warning("dead-letter okunamadı: %r", exc)
    return {"items": items, "count": len(items)}


@app.get("/health")
def health() -> dict:
    """Derin health-check: Redis ve PostgreSQL erişilebilirliğini kontrol et."""
    checks = {
        "redis": False,
        "database": False,
    }
    try:
        from celery.app.control import Control
        ctrl = Control(celery_app)
        checks["redis"] = ctrl.ping() is not None
    except Exception as exc:
        logger.debug("redis health check hatası: %r", exc)
        # Broker'a direkt ping deneyelim
        try:
            checks["redis"] = celery_app.connection().heartbeat_check()
        except Exception:
            checks["redis"] = False

    try:
        checks["database"] = ping_db()
    except Exception as exc:
        logger.debug("db health check hatası: %r", exc)
        checks["database"] = False

    healthy = all(checks.values())
    response = {
        "status": "healthy" if healthy else "unhealthy",
        "target": settings.base_url,
        "dedup_backend": dedup.backend,
        "checks": checks,
    }
    status_code = 200 if healthy else 503
    return JSONResponse(content=response, status_code=status_code)


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus_metrics() -> PlainTextResponse:
    return PlainTextResponse(content=metrics.render(), media_type="text/plain; version=0.0.4")
