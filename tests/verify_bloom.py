"""
Canlı RedisBloom doğrulama scripti (test paketinin parçası değil; elle çalıştırılır).

Amaç: core/dedup.py'ın GERÇEK RedisBloom yoluna (BF.INSERT / BF.EXISTS + anahtar ömrü)
bağlandığını ve mantığın doğru çalıştığını canlı bir Redis Stack'e karşı kanıtlamak.

Kanıtladıkları:
  1. dedup.backend == "redis-bloom"  -> in-memory fallback'e DÜŞMEDİ, gerçek Redis'e bağlandı.
  2. check_and_mark(url): ilk görüşte False (yeni), ikinci görüşte True (görüldü).
  3. Düşük seviye: BF.ADD/BF.EXISTS komutlarını doğrudan çalıştırıp Redis'in
     Bloom modülünü gerçekten tanıdığını gösterir.
  4. Ömür (DEDUP_TTL=4 ile, ~6 sn sürer): t=0'da A, t=3'te B işaretlenir; B, A'nın
     filtresiyle birlikte silinmez, en az 4 sn görüldü kalır; sonunda iki kayıt da
     Redis'ten gerçekten silinir. (Ayrı dilim adları kullanır, gerçek kayıtlara dokunmaz.)

Kullanım:
    # Önce bir Redis Stack ayakta olmalı (docker compose up -d redis  ya da redis-stack-server)
    REDIS_URL=redis://localhost:6379/0 python tests/verify_bloom.py
"""
from __future__ import annotations
import os
import sys
import time

# Proje kökünü import yoluna ekle (script doğrudan çalıştırılınca da bulsun).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.config import settings          # noqa: E402
from core.dedup import dedup, _key        # noqa: E402


def main() -> int:
    failures: list[str] = []

    print(f"REDIS_URL      : {settings.redis_url}")
    print(f"dedup.backend  : {dedup.backend}")

    # --- Kanıt 1: gerçek RedisBloom yoluna bağlandık mı? ---
    if dedup.backend != "redis-bloom":
        failures.append(
            f"backend 'redis-bloom' bekleniyordu ama '{dedup.backend}' bulundu "
            f"(Redis Stack ayakta mı? REDIS_URL doğru mu?)"
        )
        print("\n[X] Redis'e bağlanılamadı, in-memory fallback'e düşüldü.")
        # Bağlanamadıysak alt testlerin anlamı kalmaz; yine de fallback mantığını gösterelim.

    # Her çalıştırmada taze bir URL kullan ki 'ilk görüş' garanti yeni olsun.
    test_url = f"https://books.toscrape.com/catalogue/verify-{os.getpid()}-{int(time.time())}.html"
    print(f"\nTest URL       : {test_url}")
    print(f"SHA1 anahtarı  : {_key(test_url)}")

    # --- Kanıt 2: ilk görüş False, ikinci görüş True ---
    first = dedup.check_and_mark(test_url)
    second = dedup.check_and_mark(test_url)
    print(f"\ncheck_and_mark #1 (yeni URL)      -> {first}   (beklenen: False)")
    print(f"check_and_mark #2 (aynı URL)      -> {second}   (beklenen: True)")
    if first is not False:
        failures.append("İlk check_and_mark False dönmeliydi (URL yeni).")
    if second is not True:
        failures.append("İkinci check_and_mark True dönmeliydi (URL zaten görüldü).")

    # --- Kanıt 3: düşük seviye BF.ADD / BF.EXISTS doğrudan çalışıyor mu? ---
    if dedup._client is not None:
        raw_key = f"verify:bf:{os.getpid()}"
        try:
            dedup._client.execute_command("BF.RESERVE", raw_key, 0.001, 1000)
        except Exception:
            pass  # zaten varsa
        exists_before = dedup._client.execute_command("BF.EXISTS", raw_key, "item-A")
        added = dedup._client.execute_command("BF.ADD", raw_key, "item-A")
        exists_after = dedup._client.execute_command("BF.EXISTS", raw_key, "item-A")
        dedup._client.delete(raw_key)  # temizlik
        print(f"\nBF.EXISTS (eklemeden önce)        -> {exists_before}   (beklenen: 0)")
        print(f"BF.ADD                            -> {added}   (beklenen: 1)")
        print(f"BF.EXISTS (ekledikten sonra)      -> {exists_after}   (beklenen: 1)")
        if not (int(exists_before) == 0 and int(added) == 1 and int(exists_after) == 1):
            failures.append("Düşük seviye BF.ADD/BF.EXISTS beklenen değerleri vermedi.")
    else:
        print("\n[i] Düşük seviye BF testi atlandı (Redis istemcisi yok).")

    # --- Kanıt 4: ömür URL başına (dilimli), sonunda silinir ---
    if dedup._client is not None:
        failures.extend(_check_lifetime())
    else:
        print("\n[i] Ömür testi atlandı (Redis istemcisi yok).")

    # --- Sonuç ---
    print("\n" + "=" * 50)
    if failures:
        print("SONUÇ: BAŞARISIZ")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SONUC: BASARILI  Gercek RedisBloom yolu dogrulandi.")
    return 0


def _check_lifetime() -> list[str]:
    from core.dedup import Deduplicator

    ttl, old_ttl = 4, settings.dedup_ttl_sec
    settings.dedup_ttl_sec = ttl
    try:
        d = Deduplicator()
        w = d._slice_width(ttl)
        tag = f"verify-{os.getpid()}-{int(time.time())}"
        a, b = f"https://verify.example/{tag}/A", f"https://verify.example/{tag}/B"
        t0 = time.time()
        d.mark(a)
        time.sleep(3)
        d.mark(b)
        tb = time.time()
        rows, errors = [], []
        while time.time() - tb < ttl + w + 0.5:
            age_a, age_b = time.time() - t0, time.time() - tb
            sa, sb = d.seen(a), d.seen(b)
            rows.append(f"  t={age_a:4.1f}s  seen(A)={sa!s:5}  seen(B)={sb!s:5}")
            for name, age, seen_now in (("A", age_a, sa), ("B", age_b, sb)):
                if age < ttl - 0.05 and not seen_now:
                    errors.append(f"{name} {age:.1f} sn'de unutuldu (DEDUP_TTL={ttl})")
                if age > ttl + w + 0.05 and seen_now:
                    errors.append(f"{name} {age:.1f} sn'de hâlâ görüldü (üst sınır {ttl + w})")
            time.sleep(0.5)
        left = d._client.keys(f"{d.BLOOM_NAME}:{w}:*")
        print(f"\nÖmür (DEDUP_TTL={ttl}, dilim={w} sn, beklenen ömür {ttl}-{ttl + w} sn):")
        print("\n".join(rows))
        print(f"Kalan dilim anahtarı: {len(left)}   (beklenen: 0)")
        if left:
            errors.append(f"süre dolduğu halde {len(left)} dilim anahtarı Redis'te kaldı")
        return errors
    finally:
        settings.dedup_ttl_sec = old_ttl


if __name__ == "__main__":
    raise SystemExit(main())
