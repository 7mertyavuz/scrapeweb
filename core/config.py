"""
Merkezi konfigürasyon.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [p.strip().rstrip("/") for p in raw.split(",") if p.strip()]


# Eski sürümlerde README'de yayımlanmış, herkesin bildiği sabit anahtarlar.
# Ortamda açıkça bunlardan biri verilse bile kabul edilmez.
KNOWN_PUBLIC_API_KEYS = frozenset({"BENIM_GIZLI_SIFREM_123"})

@dataclass
class Settings:
    base_url: str = os.getenv("SCRAPE_BASE_URL", "https://books.toscrape.com/")
    # --- Güvenlik ---
    # API_KEY'in kullanılabilir varsayılanı yoktur: boşsa korumalı uçlar hiçbir
    # isteği kabul etmez (401). Worker ve run_local.py anahtar kullanmaz.
    api_key: str = field(default_factory=lambda: os.getenv("API_KEY", "").strip())
    # true: GET /status, /stats, /books, /dead-letter anahtarsız okunabilir (eski davranış).
    public_read: bool = field(default_factory=lambda: _env_bool("PUBLIC_READ", False))
    # Virgülle ayrılmış izinli origin listesi; boş = çapraz origin kapalı.
    cors_origins: list[str] = field(default_factory=lambda: _env_list("CORS_ORIGINS"))
    # true: iç/özel ağ adreslerine kazıma ve webhook izni (yalnız yerel geliştirme).
    allow_private_networks: bool = field(default_factory=lambda: _env_bool("ALLOW_PRIVATE_NETWORKS", False))
    # API'de kabul edilen en büyük max_pages değeri.
    max_pages_limit: int = field(default_factory=lambda: max(1, _env_int("MAX_PAGES_LIMIT", 100)))
    proxy_tier_1: str = os.getenv("PROXY_TIER_1", "") 
    proxy_tier_2: str = os.getenv("PROXY_TIER_2", "") 

    request_delay_sec: float = float(os.getenv("REQUEST_DELAY", "0.5"))
    domain_delays_raw: str = os.getenv("DOMAIN_DELAYS", "")
    max_concurrency: int = int(os.getenv("MAX_CONCURRENCY", "4"))
    request_timeout: float = float(os.getenv("REQUEST_TIMEOUT", "20"))
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))
    user_agent: str = os.getenv("USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36")
    respect_robots: bool = os.getenv("RESPECT_ROBOTS", "true").lower() == "true"

    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    log_file: str = os.getenv("LOG_FILE", "")

    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///scrapehub.db")

    dedup_ttl_sec: int = int(os.getenv("DEDUP_TTL", str(60 * 60 * 24)))
    bloom_capacity: int = int(os.getenv("BLOOM_CAPACITY", "1000000"))
    bloom_error_rate: float = float(os.getenv("BLOOM_ERROR_RATE", "0.001"))

    incremental: bool = os.getenv("INCREMENTAL", "false").lower() == "true"
    content_hash_ttl_sec: int = int(os.getenv("CONTENT_HASH_TTL", str(60 * 60 * 24 * 7)))

    # --- YENİ EKLENENLER (Konu 1, 2, 3, 5) ---
    browserless_url: str = os.getenv("BROWSERLESS_URL", "ws://localhost:3000")
    # Tarayıcının tüm trafiğini yolladığı SSRF denetimli çıkış proxy'si (core/egress_proxy.py),
    # TARAYICININ gördüğü adresle; compose'da http://egress:8899. Boşsa yalnız URL süzgeci çalışır.
    browser_egress_proxy: str = field(default_factory=lambda: os.getenv("BROWSER_EGRESS_PROXY", "").strip())
    capsolver_key: str = os.getenv("CAPSOLVER_KEY", "") # Opsiyonel
    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    jaeger_endpoint: str = os.getenv("JAEGER_ENDPOINT", "http://localhost:4318/v1/traces")

    @property
    def api_key_configured(self) -> bool:
        """Kimlik doğrulama için kullanılabilir bir anahtar var mı."""
        return bool(self.api_key) and self.api_key not in KNOWN_PUBLIC_API_KEYS

    def __post_init__(self) -> None:
        self.domain_delays: dict[str, float] = self._parse_domain_delays(self.domain_delays_raw)

    @staticmethod
    def _parse_domain_delays(raw: str) -> dict[str, float]:
        result: dict[str, float] = {}
        for part in raw.split(","):
            part = part.strip()
            if not part or ":" not in part:
                continue
            domain, _, val = part.rpartition(":")
            try:
                result[domain.strip().lower()] = float(val.strip())
            except ValueError:
                continue
        return result

    def request_delay_for(self, url: str) -> float:
        host = (urlparse(url).hostname or "").lower()
        if host in self.domain_delays:
            return self.domain_delays[host]
        if host.startswith("www.") and host[4:] in self.domain_delays:
            return self.domain_delays[host[4:]]
        return self.request_delay_sec

settings = Settings()
