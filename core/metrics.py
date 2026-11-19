"""
Hafif, process'ler arası paylaşımlı metrik toplayıcı.

Neden prometheus_client değil? API (FastAPI) ile worker'lar (Celery) AYRI
process'lerdir. prometheus_client'in sayaçları process-local'dir: worker'da
artırılan bir sayaç API'nin /metrics çıktısında görünmez. Bu yüzden sayaçları
Redis'te tutuyoruz (INCR ile atomik, tüm process'ler aynı değeri görür); Redis
yoksa in-memory fallback'e düşeriz (tek-process ders/test senaryosu).

Çıktı yine de Prometheus *text exposition* formatında verilir (render()),
böylece gerçek bir Prometheus sunucusu /metrics'i sorunsuz scrape edebilir.
"""
from __future__ import annotations
import json

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)

_PREFIX = "scrapehub:metrics:"

# Dead-letter listesinde tutulan en fazla kayıt (en yeniler kalır).
DEAD_LETTER_MAX = 1000

# Lifetime sayaçlar (monoton artan = Prometheus 'counter').
COUNTERS = ("pages_processed", "failed_fetch")

# Prometheus metrik adı + yardım metni (render için).
_META = {
    "pages_processed": ("scrapehub_pages_processed_total", "counter", "Başarıyla işlenen kitap sayfası sayısı"),
    "failed_fetch": ("scrapehub_failed_fetch_total", "counter", "Başarısız fetch sayısı"),
    "dead_letter_depth": ("scrapehub_dead_letter_depth", "gauge", "Dead-letter kuyruğundaki bekleyen görev sayısı"),
    "db_books": ("scrapehub_db_books", "gauge", "Veritabanındaki kitap kaydı sayısı"),
}


class Metrics:
    DEAD_LETTER_KEY = "scrapehub:dead_letter"

    def __init__(self) -> None:
        self._client = None
        self._mem: dict[str, int] = {name: 0 for name in COUNTERS}
        try:
            import redis  # type: ignore
            client = redis.Redis.from_url(settings.redis_url, decode_responses=True,
                                          socket_connect_timeout=2)
            client.ping()
            self._client = client
        except Exception:
            # Redis yok -> in-memory fallback (tek process için yeterli).
            self._client = None

    def inc(self, name: str, amount: int = 1) -> None:
        """Bir lifetime sayacını artır (Redis INCR, yoksa in-memory)."""
        if name not in COUNTERS:
            return
        if self._client is not None:
            try:
                self._client.incrby(_PREFIX + name, amount)
                return
            except Exception:
                pass
        self._mem[name] = self._mem.get(name, 0) + amount

    def _counter_value(self, name: str) -> int:
        if self._client is not None:
            try:
                val = self._client.get(_PREFIX + name)
                return int(val) if val is not None else 0
            except Exception:
                pass
        return self._mem.get(name, 0)

    def _dead_letter_depth(self) -> int:
        if self._client is not None:
            try:
                return int(self._client.llen(self.DEAD_LETTER_KEY))
            except Exception:
                pass
        return 0

    # --- Dead-letter listesi (Redis listesi; en eski başta, en yeni sonda) ---
    def push_dead_letter(self, payload: dict) -> bool:
        """Kaydı listeye ekle ve listeyi DEAD_LETTER_MAX ile sınırla.

        Redis yoksa ya da yazılamazsa False döner (çağıran loga düşer).
        """
        if self._client is None:
            return False
        try:
            self._client.rpush(self.DEAD_LETTER_KEY, json.dumps(payload, ensure_ascii=False))
            self._client.ltrim(self.DEAD_LETTER_KEY, -DEAD_LETTER_MAX, -1)
            return True
        except Exception as exc:
            logger.warning("dead-letter yazılamadı: %r", exc)
            return False

    def dead_letters(self, limit: int) -> list[dict]:
        """En yeni `limit` kaydı, en yeniden eskiye doğru döndür."""
        if self._client is None or limit <= 0:
            return []
        try:
            raw_items = self._client.lrange(self.DEAD_LETTER_KEY, -limit, -1)
        except Exception as exc:
            logger.warning("dead-letter okunamadı: %r", exc)
            return []
        items: list[dict] = []
        for raw in reversed(raw_items):
            try:
                items.append(json.loads(raw))
            except (TypeError, ValueError):
                continue
        return items

    def snapshot(self) -> dict[str, int]:
        """Tüm sayaç + gauge'ların anlık değerleri."""
        # DB sayımı okuma anında hesaplanır (gauge).
        try:
            from storage.db import count_books
            db_books = count_books()
        except Exception as e:
            logger.warning("metrics: db_books okunamadı: %r", e)
            db_books = 0
        data = {name: self._counter_value(name) for name in COUNTERS}
        data["dead_letter_depth"] = self._dead_letter_depth()
        data["db_books"] = db_books
        return data

    def render(self) -> str:
        """Prometheus text exposition formatında çıktı üret."""
        snap = self.snapshot()
        lines: list[str] = []
        for key, value in snap.items():
            metric, mtype, help_text = _META[key]
            lines.append(f"# HELP {metric} {help_text}")
            lines.append(f"# TYPE {metric} {mtype}")
            lines.append(f"{metric} {value}")
        return "\n".join(lines) + "\n"


# tek paylaşımlı örnek
metrics = Metrics()
