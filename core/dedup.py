"""
Katman 2: Tekilleştirme (Deduplication).

Senin orijinal planındaki Redis Bloom Filter mantığı birebir burada.
Amaç: bir URL yakın zamanda (DEDUP_TTL, varsayılan 24 saat) başarıyla işlendiyse
tekrar çekme.

İki mod:
  1. Redis Stack varsa  -> gerçek RedisBloom (BF.INSERT / BF.EXISTS) - O(1), RAM'de, devasa ölçekli.
     Bloom filtresinden tek öğe silinemez; bu yüzden ömür FİLTRENİN tamamına verilir:
     filtre ilk işaretten DEDUP_TTL saniye sonra toptan silinir ve yeniden başlar
     (URL başına değil, pencere başına süre).
  2. Redis yoksa        -> in-memory set fallback (tek makinede ders/test için yeterli);
     burada süre URL başınadır.

Bloom filter neden? 500k URL'i bir SQL tablosunda "SELECT ... WHERE url=?" ile
kontrol etmek pahalı. Bloom filter nanosaniyede "kesinlikle yok" veya "muhtemelen var"
cevabı verir. False-positive oranı ayarlanabilir (default %0.1).
"""
from __future__ import annotations
import hashlib
import time
from typing import Optional

from core.config import settings

try:
    import redis  # type: ignore
    _redis_available = True
except ImportError:
    _redis_available = False


def _key(url: str) -> str:
    """URL'i normalize edip sabit uzunlukta bir anahtara çevirir."""
    return hashlib.sha1(url.strip().lower().encode()).hexdigest()


class Deduplicator:
    """URL daha önce işlendi mi? sorusunu cevaplar."""

    BLOOM_NAME = "scrapehub:seen_urls"

    CHASH_PREFIX = "scrapehub:chash:"

    def __init__(self) -> None:
        self._client: Optional["redis.Redis"] = None
        self._mem: set[str] = set()        # fallback
        self._mem_ttl: dict[str, float] = {}
        self._mem_chash: dict[str, str] = {}   # incremental: url-key -> content hash (fallback)
        self.backend = "memory"

        if _redis_available:
            try:
                self._client = redis.Redis.from_url(
                    settings.redis_url, decode_responses=True
                )
                self._client.ping()
                self._ensure_bloom()
                self.backend = "redis-bloom"
            except Exception:
                # Redis ayakta değilse sessizce memory moduna düş.
                self._client = None

    def _ensure_bloom(self) -> None:
        """RedisBloom filtresini bir kez reserve et (zaten varsa hatayı yut)."""
        try:
            self._client.execute_command(
                "BF.RESERVE",
                self.BLOOM_NAME,
                settings.bloom_error_rate,
                settings.bloom_capacity,
            )
        except Exception:
            pass  # zaten reserve edilmiş

    def seen(self, url: str) -> bool:
        """Bu URL daha önce görüldüyse True döner."""
        k = _key(url)
        if self._client is not None:
            try:
                return bool(self._client.execute_command("BF.EXISTS", self.BLOOM_NAME, k))
            except Exception:
                pass
        # memory fallback (TTL kontrollü)
        now = time.time()
        exp = self._mem_ttl.get(k)
        if exp and exp < now:
            self._mem.discard(k)
            self._mem_ttl.pop(k, None)
            return False
        return k in self._mem

    def mark(self, url: str) -> None:
        """URL'i 'görüldü' olarak işaretle."""
        k = _key(url)
        if self._client is not None:
            try:
                # BF.INSERT, filtre yoksa (ör. TTL ile silindiyse) onu ayarlı kapasite ve
                # hata oranıyla yeniden kurar; BF.ADD varsayılan küçük parametreler kullanırdı.
                self._client.execute_command(
                    "BF.INSERT", self.BLOOM_NAME,
                    "CAPACITY", settings.bloom_capacity,
                    "ERROR", settings.bloom_error_rate,
                    "ITEMS", k,
                )
                self._ensure_ttl()
                return
            except Exception:
                pass
        self._mem.add(k)
        self._mem_ttl[k] = time.time() + settings.dedup_ttl_sec

    def _ensure_ttl(self) -> None:
        """Filtrenin ömrü yoksa DEDUP_TTL ver; varsa uzatma (pencere ilk işaretten başlar)."""
        try:
            if self._client.ttl(self.BLOOM_NAME) == -1:   # -1: anahtar var, ömrü yok
                self._client.expire(self.BLOOM_NAME, settings.dedup_ttl_sec)
        except Exception:
            pass  # ömür verilemese de işaret yazıldı

    def check_and_mark(self, url: str) -> bool:
        """
        Görülmediyse işaretle ve False döndür (=işle), görülmüşse True döndür (=atla).
        (Atomik değildir: seen + mark iki ayrı adımdır.)

        Not: görevler bunu kullanmaz; URL'yi ancak başarılı yazımdan sonra mark() ile
        işaretler, böylece başarısız bir deneme URL'yi "görüldü" bırakmaz.
        """
        if self.seen(url):
            return True
        self.mark(url)
        return False

    # --- Incremental (artımlı) güncelleme: içerik-hash karşılaştırma ---
    def content_changed(self, url: str, html: str, *, remember: bool = True) -> bool:
        """
        Sayfanın İÇERİĞİ son görülenden farklı mı?
          - Yeni URL ya da içerik değişmiş  -> True  (yeniden işle) + (remember ise) yeni hash'i sakla.
          - İçerik aynı                      -> False (atla).

        URL-bazlı bloom dedup "bu URL'i hiç gördük mü?" sorusuna bakar; bu metot ise
        "gördüğümüzden bu yana sayfa değişti mi?" sorusuna bakar (incremental güncelleme).

        remember=False: yalnız karşılaştırır; hash'i iş başarıyla bitince
        remember_content() ile saklamak çağıranın işidir (ayrıştırma/DB hatasında
        sayfa bir sonraki koşuda "değişmemiş" sayılıp atlanmasın diye).
        """
        new_hash = _content_hash(html)
        k = _key(url)

        if self._client is not None:
            try:
                prev = self._client.get(self.CHASH_PREFIX + k)
                if prev == new_hash:
                    return False
                if remember:
                    self.remember_content(url, html)
                return True
            except Exception:
                pass  # redis sorunlu -> memory'e düş

        prev = self._mem_chash.get(k)
        if prev == new_hash:
            return False
        if remember:
            self._mem_chash[k] = new_hash
        return True

    def remember_content(self, url: str, html: str) -> None:
        """Sayfanın içerik hash'ini 'son işlenen sürüm' olarak sakla."""
        new_hash = _content_hash(html)
        k = _key(url)
        if self._client is not None:
            try:
                self._client.set(self.CHASH_PREFIX + k, new_hash, ex=settings.content_hash_ttl_sec)
                return
            except Exception:
                pass
        self._mem_chash[k] = new_hash


def _content_hash(html: str) -> str:
    return hashlib.sha1(html.encode("utf-8", "ignore")).hexdigest()


# tek paylaşımlı örnek
dedup = Deduplicator()
