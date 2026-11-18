"""
Katman 3 + 4: Ağ, Stealth Bypass ve Proxy Şelalesi (Waterfall).
"""
from __future__ import annotations
import time
import urllib.robotparser as robotparser
from functools import lru_cache
from typing import Optional
from urllib.parse import urlsplit

from curl_cffi import requests

from core.config import settings
from core.logging import get_logger
from core.netguard import UnsafeURLError, guarded_request

logger = get_logger(__name__)

@lru_cache(maxsize=256)
def _robot_parser(origin: str) -> robotparser.RobotFileParser:
    """`origin` (scheme://host[:port]) için robots.txt'i çek ve ayrıştır.

    İstek, sayfa istekleri gibi netguard üzerinden gider (SSRF koruması).
    Kurallar urllib.robotparser ile aynı: 401/403 -> her şey yasak, diğer 4xx ->
    her şey serbest. 5xx ya da ağ hatası istisna fırlatır; lru_cache istisnaları
    önbelleğe almadığından bir sonraki çağrıda yeniden denenir.
    """
    resp = guarded_request(
        "GET", origin + "/robots.txt", impersonate="chrome120", timeout=settings.request_timeout
    )
    rp = robotparser.RobotFileParser(origin + "/robots.txt")
    if resp.status_code in (401, 403):
        rp.disallow_all = True
    elif 400 <= resp.status_code < 500:
        rp.allow_all = True
    elif resp.status_code == 200:
        rp.parse(resp.text.splitlines())
    else:
        raise RuntimeError(f"robots.txt alınamadı: HTTP {resp.status_code}")
    return rp

def allowed_by_robots(url: str) -> bool:
    """URL'nin KENDİ alan adının robots.txt kurallarına göre izinli mi?"""
    if not settings.respect_robots:
        return True
    parts = urlsplit(url)
    try:
        rp = _robot_parser(f"{parts.scheme}://{parts.netloc}".lower())
    except Exception as exc:
        # Okunamayan robots.txt kazımayı kalıcı olarak durdurmasın (önbelleğe girmez).
        # İç/özel adres ise sayfa isteği de netguard'da durur.
        logger.warning("[robots] okunamadı, izinli sayılıyor: %s (%r)", url, exc)
        return True
    return rp.can_fetch("*", url) # Stealth için wildcard

class Fetcher:
    def get(self, url: str) -> Optional[str]:
        if not allowed_by_robots(url):
            logger.warning("[robots] izin yok, atlanıyor: %s", url)
            return None

        delay = settings.request_delay_for(url)
        last_err: Optional[Exception] = None

        # --- PROXY ŞELALESİ (WATERFALL) ---
        waterfall_tiers = [(None, "chrome120")] # Tier 0: Kendi IP'miz
        
        if settings.proxy_tier_1:
            waterfall_tiers.append((settings.proxy_tier_1, "chrome120"))
        if settings.proxy_tier_2:
            waterfall_tiers.append((settings.proxy_tier_2, "safari15_3"))

        current_tier_idx = 0
        last_status: Optional[int] = None

        def backoff(attempt: int, reason: str) -> None:
            # Son denemeden sonra beklemek boşa zaman: döngü zaten bitiyor.
            if attempt >= settings.max_retries:
                return
            wait = delay * (2 ** attempt)
            logger.warning("[retry %d/%d] %s -> %.1fs bekleniyor", attempt, settings.max_retries, reason, wait)
            time.sleep(wait)

        for attempt in range(1, settings.max_retries + 1):
            current_proxy, impersonate_browser = waterfall_tiers[current_tier_idx]
            proxies = {"http": current_proxy, "https": current_proxy} if current_proxy else None

            try:
                # SSRF korumalı istek: hedef ve her yönlendirme adımı doğrulanır,
                # doğrulanan IP curl'e sabitlenir (bkz. core/netguard.py).
                resp = guarded_request(
                    "GET",
                    url,
                    impersonate=impersonate_browser,
                    proxies=proxies,
                    timeout=settings.request_timeout
                )
                time.sleep(delay)

                if resp.status_code == 200:
                    return resp.text

                last_status = resp.status_code
                # WAF Engeliyse proxy yükselt (403/429/503)
                if resp.status_code in (403, 429, 503):
                    logger.warning("[WAF Block] %s %s", resp.status_code, url)
                    if current_tier_idx < len(waterfall_tiers) - 1:
                        current_tier_idx += 1
                        logger.info("Proxy seviyesi artırıldı -> Tier %d", current_tier_idx)
                    else:
                        backoff(attempt, "Tüm proxyler banlandı")
                    continue

                if 400 <= resp.status_code < 500:
                    logger.warning("[skip] %s %s", resp.status_code, url)
                    return None

                # 5xx (503 dışı) ve beklenmeyen kodlar: sunucu sorunu, geri çekilip yeniden dene.
                backoff(attempt, f"HTTP {resp.status_code} {url}")

            except UnsafeURLError as e:
                # Politika ihlali kalıcıdır: yeniden deneme ya da proxy yükseltme yok.
                logger.warning("[ssrf] engellendi: %s (%s)", url, e)
                return None
            except requests.RequestsError as e:
                last_err = e
                # Ağ Hatası olursa da Proxy yükselt
                if current_tier_idx < len(waterfall_tiers) - 1:
                    current_tier_idx += 1
                    logger.info("Proxy Bağlantı Hatası -> Tier %d geçiliyor", current_tier_idx)
                else:
                    backoff(attempt, "Ağ Hatası")

        if last_err:
            logger.error("[fail] %s: %r", url, last_err)
        elif last_status is not None:
            logger.error("[fail] %s: HTTP %s", url, last_status)
        return None

    def close(self) -> None:
        pass

fetcher = Fetcher()
