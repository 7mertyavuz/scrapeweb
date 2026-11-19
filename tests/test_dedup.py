"""
Dedup testi: ilk görüşte False (yeni -> işle), ikinci görüşte True (görüldü -> atla).

Paylaşılan global `dedup` örneğini kirletmemek için her test kendi Deduplicator()
örneğini kurar. Redis ulaşılamaz olduğundan (conftest) backend 'memory' olur; mantık
her iki backend'de de aynıdır.
"""
from __future__ import annotations

import pytest

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
# Ömür: URL en az DEDUP_TTL "görüldü" kalır, en geç DEDUP_TTL + bir dilim sonra
# unutulur; tüm durum aynı anda sıfırlanmaz. İki backend de aynı kurala uyar.
# Saat elle sürülür (d._clock); Redis tarafı, anahtar ömrünü aynı saatle uygulayan
# küçük bir RedisBloom taklidiyle denenir. Gerçek Redis ölçümü: tests/verify_bloom.py.
# --------------------------------------------------------------------------
class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeBloomRedis:
    """RedisBloom + anahtar ömrü (EXPIRE/PEXPIRE) davranışının saatli küçük taklidi."""

    def __init__(self, clock: Clock):
        self.clock = clock
        self.filters: dict[str, dict] = {}     # ad -> {"items": set, "capacity", "error"}
        self.expire_at: dict[str, float] = {}  # ad -> mutlak saniye

    def _purge(self) -> None:
        now = self.clock()
        for name, at in list(self.expire_at.items()):
            if at <= now:                       # ömür doldu: Redis anahtarı siler
                self.filters.pop(name, None)
                self.expire_at.pop(name, None)

    def keys(self) -> list[str]:
        self._purge()
        return sorted(self.filters)

    def execute_command(self, cmd, name, *args):
        self._purge()
        cmd = cmd.upper()
        if cmd == "BF.RESERVE":
            if name in self.filters:
                raise Exception("ERR item exists")
            self._create(name, float(args[0]), int(args[1]))
            return "OK"
        if cmd == "BF.EXISTS":
            return int(name in self.filters and args[0] in self.filters[name]["items"])
        if cmd == "BF.INSERT":
            opts = list(args)
            items = opts[opts.index("ITEMS") + 1:]
            if name not in self.filters:
                cap = int(opts[opts.index("CAPACITY") + 1]) if "CAPACITY" in opts else 100
                err = float(opts[opts.index("ERROR") + 1]) if "ERROR" in opts else 0.01
                self._create(name, err, cap)
            return [int(self._add(name, it)) for it in items]
        raise AssertionError(f"beklenmeyen komut {cmd}")

    def _create(self, name, error, capacity):
        self.filters[name] = {"items": set(), "capacity": capacity, "error": error}

    def _add(self, name, item):
        new = item not in self.filters[name]["items"]
        self.filters[name]["items"].add(item)
        return new

    def ttl(self, name):
        self._purge()
        if name not in self.filters:
            return -2
        if name not in self.expire_at:
            return -1
        return int(self.expire_at[name] - self.clock())

    def expire(self, name, seconds):
        return self.pexpire(name, int(seconds) * 1000)

    def pexpire(self, name, ms):
        self._purge()
        if name not in self.filters:
            return False
        self.expire_at[name] = self.clock() + int(ms) / 1000
        return True

    def pipeline(self, transaction=True):
        return FakePipeline(self)


class FakePipeline:
    """Komutları biriktirip execute()'ta sırayla çalıştırır (MULTI/EXEC yerine)."""

    def __init__(self, r: FakeBloomRedis):
        self.r, self.ops = r, []

    def __getattr__(self, name):
        def queue(*args):
            self.ops.append((name, args))
            return self
        return queue

    def execute(self):
        ops, self.ops = self.ops, []
        return [getattr(self.r, name)(*args) for name, args in ops]


def _memory_dedup(monkeypatch, ttl):
    from core.config import settings
    monkeypatch.setattr(settings, "dedup_ttl_sec", ttl)
    clock = Clock()
    d = Deduplicator()
    d._clock = clock
    return d, clock, None


def _bloom_dedup(monkeypatch, ttl):
    d, clock, _ = _memory_dedup(monkeypatch, ttl)
    fake = FakeBloomRedis(clock)
    d._client = fake
    d.backend = "redis-bloom"
    return d, clock, fake


BACKENDS = pytest.mark.parametrize("make", [_memory_dedup, _bloom_dedup], ids=["memory", "redis-bloom"])


@BACKENDS
def test_url_marked_late_in_window_stays_seen_for_full_ttl(monkeypatch, make):
    """Gözlenen hata: DEDUP_TTL=4; t=0'da A, t=3'te B. Eskiden t=4'te B de silindi."""
    d, clock, fake = make(monkeypatch, 4)
    t0 = clock.t
    d.mark("https://x.example/A")
    clock.t = t0 + 3
    d.mark("https://x.example/B")
    if fake is not None:
        assert fake.keys(), "Redis yolu kullanılmalı (memory'e düşmemeli)"
    clock.t = t0 + 3.99
    assert d.seen("https://x.example/A") is True     # A da en az 4 sn yaşar
    for dt in (3.0, 4.0, 5.0, 6.0, 6.99):           # B'nin işaretinden itibaren < 4 sn
        clock.t = t0 + dt
        assert d.seen("https://x.example/B") is True, f"t={dt}: B DEDUP_TTL dolmadan unutuldu"


@BACKENDS
def test_urls_are_forgotten_one_by_one_not_all_at_once(monkeypatch, make):
    """Her saniye bir URL: herhangi bir anda yalnız DEDUP_TTL'i dolmuş olanlar unutulur."""
    ttl = 8
    d, clock, _ = make(monkeypatch, ttl)
    t0 = clock.t
    marked = {}
    for i in range(12):
        clock.t = t0 + i
        url = f"https://x.example/{i}"
        d.mark(url)
        marked[url] = clock.t
    w = d._slice_width(ttl)
    for step in range(60):
        clock.t = t0 + 11 + step / 4
        for url, at in marked.items():
            age = clock.t - at
            if age < ttl:
                assert d.seen(url) is True, f"{url} yaşı {age} < TTL iken unutuldu"
            elif age >= ttl + w:
                assert d.seen(url) is False, f"{url} yaşı {age} >= TTL + {w} iken hâlâ görüldü"


@BACKENDS
def test_records_are_eventually_deleted(monkeypatch, make):
    ttl = 3600
    d, clock, fake = make(monkeypatch, ttl)
    t0 = clock.t
    for i in range(50):
        clock.t = t0 + i * 60
        d.mark(f"https://x.example/{i}")
    clock.t = t0 + 49 * 60 + ttl + d._slice_width(ttl)
    assert not any(d.seen(f"https://x.example/{i}") for i in range(50))
    d.mark("https://x.example/new")                 # bellek yolu eskileri burada atar
    if fake is not None:
        assert len(fake.keys()) == 1                 # Redis eski dilimleri sildi
        assert fake.ttl(fake.keys()[0]) > 0          # yeni dilimin de ömrü var
    else:
        assert d._mem_entries() == 1                 # bellek sınırsız büyümez


def test_bloom_slices_use_configured_params_and_always_expire(monkeypatch):
    from core.config import settings
    d, clock, fake = _bloom_dedup(monkeypatch, 3600)
    d.mark("https://x.example/a")
    (name,) = fake.keys()
    f = fake.filters[name]
    # Dilimler ayarlardan türetilir (BF.INSERT'in varsayılan 100'ü değil).
    assert f["capacity"] >= settings.bloom_capacity // Deduplicator.SLICES
    assert f["error"] <= settings.bloom_error_rate
    assert 3600 <= fake.ttl(name) <= 3600 + d._slice_width(3600)


def test_content_changed_can_check_without_remembering():
    d = Deduplicator()
    url = "https://books.toscrape.com/a"
    assert d.content_changed(url, "v1", remember=False) is True
    assert d.content_changed(url, "v1", remember=False) is True   # saklanmadı
    d.remember_content(url, "v1")
    assert d.content_changed(url, "v1", remember=False) is False
