"""
Katman 2: Tekilleştirme (Deduplication).

Senin orijinal planındaki Redis Bloom Filter mantığı birebir burada.
Amaç: bir URL yakın zamanda (DEDUP_TTL, varsayılan 24 saat) başarıyla işlendiyse
tekrar çekme.

İki mod:
  1. Redis Stack varsa  -> gerçek RedisBloom (BF.INSERT / BF.EXISTS) - O(1), RAM'de, devasa ölçekli.
  2. Redis yoksa        -> in-memory fallback (tek makinede ders/test için yeterli).

Ömür (iki modda aynı): Bloom filtresinden tek öğe silinemez, bu yüzden zaman
SLICES dilime bölünür (dilim genişliği w = ceil(DEDUP_TTL / SLICES) sn). Her dilimin
kendi filtresi vardır; URL, işaretlendiği andaki dilime yazılır ve o dilim, dilimin
bitişinden DEDUP_TTL sonra silinir (Redis'te anahtar ömrü). seen() hâlâ yaşayan
dilimlerin hepsine bakar. Sonuç:
  - işaretlenen URL en az DEDUP_TTL, en çok DEDUP_TTL + w saniye "görüldü" kalır
    (varsayılan 24 sa için 24-27 sa);
  - dilimler tek tek düşer: bir anda yalnız DEDUP_TTL'ini doldurmuş kayıtlar unutulur,
    tüm durum aynı anda sıfırlanmaz.
Dilim numarası worker'ın saatinden hesaplanır; süreçler arası tutarlılık için worker
saatlerinin (NTP ile) senkron olduğu varsayılır.

Bloom filter neden? 500k URL'i bir SQL tablosunda "SELECT ... WHERE url=?" ile
kontrol etmek pahalı. Bloom filter nanosaniyede "kesinlikle yok" veya "muhtemelen var"
cevabı verir. False-positive oranı ayarlanabilir (default %0.1).
"""
from __future__ import annotations
import hashlib
import math
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

    # Dilim filtrelerinin adı: scrapehub:seen_urls:<w>:<dilim no>. Genişliği (w) ada
    # katmak, DEDUP_TTL değişince eski numaralandırmayla karışmayı önler.
    BLOOM_NAME = "scrapehub:seen_urls"
    SLICES = 8   # büyüdükçe ömür üst sınırı TTL'e yaklaşır, seen() daha çok filtreye bakar

    CHASH_PREFIX = "scrapehub:chash:"

    def __init__(self) -> None:
        self._client: Optional["redis.Redis"] = None
        self._clock = time.time                       # testlerde elle sürülür
        self._mem_slices: dict[tuple[int, int], set[str]] = {}   # fallback: (w, dilim) -> anahtarlar
        self._mem_chash: dict[str, str] = {}   # incremental: url-key -> content hash (fallback)
        self.backend = "memory"

        if _redis_available:
            try:
                self._client = redis.Redis.from_url(
                    settings.redis_url, decode_responses=True
                )
                self._client.ping()
                self.backend = "redis-bloom"
            except Exception:
                # Redis ayakta değilse sessizce memory moduna düş.
                self._client = None

    # --- Zaman dilimleri ---
    def _slice_width(self, ttl: int) -> int:
        return max(1, math.ceil(ttl / self.SLICES))

    def _window(self) -> tuple[int, int, range, float]:
        """(ttl, w, şu an yaşayan dilim numaraları, şimdi)."""
        ttl = max(0, int(settings.dedup_ttl_sec))
        w = self._slice_width(ttl)
        now = self._clock()
        # Dilim s, (s+1)*w + ttl anına kadar yaşar.
        return ttl, w, range(math.floor((now - ttl) / w), math.floor(now / w) + 1), now

    def _slice_name(self, w: int, s: int) -> str:
        return f"{self.BLOOM_NAME}:{w}:{s}"

    def seen(self, url: str) -> bool:
        """Bu URL son DEDUP_TTL içinde işaretlendiyse True döner."""
        k = _key(url)
        _, w, alive, _ = self._window()
        if self._client is not None:
            try:
                pipe = self._client.pipeline(transaction=False)   # tek gidiş-dönüş
                for s in alive:
                    pipe.execute_command("BF.EXISTS", self._slice_name(w, s), k)
                return any(int(r) for r in pipe.execute())
            except Exception:
                pass
        return any(k in self._mem_slices.get((w, s), ()) for s in alive)

    def mark(self, url: str) -> None:
        """URL'i 'görüldü' olarak işaretle (şu anki dilime yazar)."""
        k = _key(url)
        ttl, w, alive, now = self._window()
        s = alive[-1]
        if self._client is not None:
            try:
                # Dilim, bitişinden ttl sonra silinir. Göreli süre (PEXPIRE) verilir ki
                # Redis sunucusunun saatiyle istemcininki arasındaki fark ömrü kaydırmasın.
                expire_ms = max(1, math.ceil(((s + 1) * w + ttl - now) * 1000))
                name = self._slice_name(w, s)
                pipe = self._client.pipeline(transaction=True)    # MULTI/EXEC: ömürsüz filtre kalmasın
                # BF.INSERT filtreyi yoksa kurar. Toplam bellek ve birleşik yanlış-pozitif
                # oranı tek filtreye yakın kalsın diye kapasite dilimlere bölünür (aşılırsa
                # RedisBloom kendini büyütür), hata oranı bakılan dilim sayısına bölünür.
                pipe.execute_command(
                    "BF.INSERT", name,
                    "CAPACITY", math.ceil(settings.bloom_capacity / self.SLICES),
                    "ERROR", settings.bloom_error_rate / (self.SLICES + 1),
                    "ITEMS", k,
                )
                pipe.pexpire(name, expire_ms)
                pipe.execute()
                return
            except Exception:
                pass
        # Bellek: aynı dilim düzeni; artık yaşamayan dilimler burada atılır.
        for old in [key for key in self._mem_slices if key[0] != w or key[1] not in alive]:
            del self._mem_slices[old]
        self._mem_slices.setdefault((w, s), set()).add(k)

    def _mem_entries(self) -> int:
        """Bellek yedeğinde tutulan kayıt sayısı (test/teşhis için)."""
        return sum(len(v) for v in self._mem_slices.values())

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
