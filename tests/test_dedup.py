"""
Dedup testi: ilk görüşte False (yeni -> işle), ikinci görüşte True (görüldü -> atla).

Paylaşılan global `dedup` örneğini kirletmemek için her test kendi Deduplicator()
örneğini kurar. Redis ulaşılamaz olduğundan (conftest) backend 'memory' olur; mantık
her iki backend'de de aynıdır.
"""
from __future__ import annotations

from core.dedup import Deduplicator


def test_backend_is_memory_without_redis():
    d = Deduplicator()
    assert d.backend == "memory"


def test_check_and_mark_first_false_second_true():
    d = Deduplicator()
    url = "https://books.toscrape.com/catalogue/some-book_1/index.html"
    assert d.check_and_mark(url) is False   # ilk görüş -> işle
    assert d.check_and_mark(url) is True    # ikinci görüş -> atla


def test_seen_reflects_mark():
    d = Deduplicator()
    url = "https://books.toscrape.com/catalogue/other-book_2/index.html"
    assert d.seen(url) is False
    d.mark(url)
    assert d.seen(url) is True


def test_distinct_urls_independent():
    d = Deduplicator()
    u1 = "https://books.toscrape.com/a"
    u2 = "https://books.toscrape.com/b"
    assert d.check_and_mark(u1) is False
    assert d.check_and_mark(u2) is False   # farklı URL etkilenmez
    assert d.check_and_mark(u1) is True
    assert d.check_and_mark(u2) is True


def test_key_normalizes_case_and_whitespace():
    from core.dedup import _key
    assert _key("  HTTPS://Books.Toscrape.com/X  ") == _key("https://books.toscrape.com/x")


# --------------------------------------------------------------------------
# Redis Bloom yolu: DEDUP_TTL orada da geçerli olmalı (sahte Redis ile)
# --------------------------------------------------------------------------
class FakeBloomRedis:
    """RedisBloom + anahtar ömrü davranışının küçük taklidi."""

    def __init__(self):
        self.filters: dict[str, dict] = {}   # ad -> {"items": set, "capacity", "error"}
        self.expiry: dict[str, int] = {}     # ad -> saniye

    def _create(self, name, error=0.01, capacity=100):
        self.filters[name] = {"items": set(), "capacity": capacity, "error": error}

    def execute_command(self, cmd, name, *args):
        cmd = cmd.upper()
        if cmd == "BF.RESERVE":
            if name in self.filters:
                raise Exception("ERR item exists")
            self._create(name, float(args[0]), int(args[1]))
            return "OK"
        if cmd == "BF.EXISTS":
            return int(name in self.filters and args[0] in self.filters[name]["items"])
        if cmd == "BF.ADD":                     # yoksa varsayılan parametrelerle oluşturur
            if name not in self.filters:
                self._create(name)
            return int(self._add(name, args[0]))
        if cmd == "BF.INSERT":
            opts = list(args)
            items = opts[opts.index("ITEMS") + 1:]
            if name not in self.filters:
                cap = int(opts[opts.index("CAPACITY") + 1]) if "CAPACITY" in opts else 100
                err = float(opts[opts.index("ERROR") + 1]) if "ERROR" in opts else 0.01
                self._create(name, err, cap)
            return [int(self._add(name, it)) for it in items]
        raise AssertionError(f"beklenmeyen komut {cmd}")

    def _add(self, name, item):
        new = item not in self.filters[name]["items"]
        self.filters[name]["items"].add(item)
        return new

    def ttl(self, name):
        if name not in self.filters:
            return -2
        return self.expiry.get(name, -1)

    def expire(self, name, seconds):
        if name not in self.filters:
            return False
        self.expiry[name] = int(seconds)
        return True

    def expire_now(self, name):
        """Anahtar ömrü doldu: Redis anahtarı siler."""
        self.filters.pop(name, None)
        self.expiry.pop(name, None)


def _bloom_dedup(monkeypatch, ttl=3600):
    from core.config import settings
    monkeypatch.setattr(settings, "dedup_ttl_sec", ttl)
    d = Deduplicator()
    fake = FakeBloomRedis()
    d._client = fake
    d._ensure_bloom()
    d.backend = "redis-bloom"
    return d, fake


def test_bloom_filter_gets_dedup_ttl(monkeypatch):
    d, fake = _bloom_dedup(monkeypatch, ttl=3600)
    d.mark("https://books.toscrape.com/a")
    assert fake.ttl(Deduplicator.BLOOM_NAME) == 3600
    assert d.seen("https://books.toscrape.com/a") is True


def test_bloom_ttl_is_not_extended_by_later_marks(monkeypatch):
    """Pencere ilk işaretten başlar; her mark ömrü uzatsaydı filtre hiç sıfırlanmazdı."""
    d, fake = _bloom_dedup(monkeypatch, ttl=3600)
    d.mark("https://books.toscrape.com/a")
    fake.expiry[Deduplicator.BLOOM_NAME] = 10        # zaman geçti, 10 sn kaldı
    d.mark("https://books.toscrape.com/b")
    assert fake.ttl(Deduplicator.BLOOM_NAME) == 10


def test_bloom_expiry_forgets_urls_and_recreates_with_configured_params(monkeypatch):
    from core.config import settings
    d, fake = _bloom_dedup(monkeypatch, ttl=3600)
    url = "https://books.toscrape.com/a"
    d.mark(url)
    fake.expire_now(Deduplicator.BLOOM_NAME)
    assert d.seen(url) is False                      # TTL doldu -> yeniden işlenebilir

    d.mark(url)                                      # filtre yeniden oluşur
    f = fake.filters[Deduplicator.BLOOM_NAME]
    assert f["capacity"] == settings.bloom_capacity  # BF.ADD'in varsayılan 100'ü değil
    assert f["error"] == settings.bloom_error_rate
    assert fake.ttl(Deduplicator.BLOOM_NAME) == 3600


def test_content_changed_can_check_without_remembering():
    d = Deduplicator()
    url = "https://books.toscrape.com/a"
    assert d.content_changed(url, "v1", remember=False) is True
    assert d.content_changed(url, "v1", remember=False) is True   # saklanmadı
    d.remember_content(url, "v1")
    assert d.content_changed(url, "v1", remember=False) is False
