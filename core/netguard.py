"""
Giden istek koruması (SSRF savunması).

Kullanıcıdan gelen her URL (kazıma hedefi, keşfedilen bağlantı, yönlendirme
hedefi, webhook) ağa çıkmadan önce buradan geçer:

* Yalnız ``http``/``https``; kullanıcı bilgisi (``user@host``), ters bölü,
  boşluk/kontrol karakteri içeren URL'ler reddedilir (ayrıştırıcı farkı hilelerine karşı).
* Ana makine adı çözülür; çözülen adreslerden BİRİ bile genel (global) değilse
  istek reddedilir: loopback, özel ağlar, link-local (bulut metadata dahil),
  CGNAT, ULA, multicast, ayrılmış aralıklar, IPv4-mapped/6to4/Teredo/NAT64
  içine gömülü iç adresler.
* Doğrulanan IP, curl'e ``CURLOPT_RESOLVE`` ile sabitlenir; böylece kontrol ile
  bağlantı arasında DNS yeniden bağlama (rebinding) yapılamaz. curl'e özgün URL
  değil, ana makine kısmı tam olarak denetlenen adla yeniden kurulmuş URL verilir
  (``host.`` / büyük harf / IDN / sayısal IP yazımları sabitlemeyi atlatamaz).
* Yönlendirmeler curl'e bırakılmaz; her adım elle izlenir ve yeniden doğrulanır.
* Yanıttan sonra curl'ün gerçekte bağlandığı IP (``primary_ip``) bir kez daha
  denetlenir (proxy yoksa).

``ALLOW_PRIVATE_NETWORKS=true`` yerel geliştirme için IP denetimlerini kapatır;
şema ve sözdizimi denetimleri her zaman çalışır.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

from curl_cffi import CurlOpt
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_exc

from core.config import settings
from core.logging import get_logger

logger = get_logger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})
MAX_URL_LENGTH = 2048
DEFAULT_MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

# Çözümlemeden önce bile reddedilen, iç ağa işaret eden ad sonekleri.
_BLOCKED_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_BLOCKED_SUFFIXES = (".localhost", ".local", ".localdomain", ".internal", ".home.arpa", ".lan", ".intranet", ".corp")

_NAT64_WKP = ipaddress.ip_network("64:ff9b::/96")
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-.")


class UnsafeURLError(ValueError):
    """URL güvenlik politikasını ihlal ediyor; istek gönderilmemeli."""


class TooManyRedirects(curl_exc.TooManyRedirects):
    """Elle izlenen yönlendirme sınırı aşıldı."""


class HostResolutionError(curl_exc.DNSError):
    """Ana makine adı doğrulama için çözülemedi.

    Geçici ağ hatası sayılır (fetcher yeniden dener); istek yine de gönderilmez.
    """


@dataclass(frozen=True)
class Target:
    url: str
    host: str          # IDNA ile ASCII'ye çevrilmiş, küçük harfli, sonda noktasız ana makine adı
    port: int
    ip_literal: bool   # host zaten bir IP adresi mi
    pinned_ip: Optional[str]  # curl'e sabitlenecek, doğrulanmış adres
    # curl'e gerçekten verilen URL: ana makine kısmı tam olarak denetlenen ``host``
    # (sondaki nokta, büyük harf, IDN, sayısal IP yazımları normalleştirilmiş).
    request_url: str = ""


def _allow_private() -> bool:
    return bool(getattr(settings, "allow_private_networks", False))


# --------------------------------------------------------------------------
# IP sınıflandırma
# --------------------------------------------------------------------------
def _embedded_ipv4(ip: ipaddress.IPv6Address) -> Optional[ipaddress.IPv4Address]:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64_WKP:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    # IPv4-compatible (::a.b.c.d, kullanımdan kalkmış)
    if int(ip) >> 32 == 0 and int(ip) > 1:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def is_public_ip(value: str | ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Adres internette genel olarak yönlendirilebilir mi (iç ağ değil mi)."""
    try:
        ip = ipaddress.ip_address(str(value).split("%", 1)[0]) if isinstance(value, str) else value
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_ipv4(ip)
        if inner is not None and not is_public_ip(inner):
            return False
        if ip.is_site_local:
            return False
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
        or ip.is_reserved or ip.is_unspecified
    ):
        return False
    return bool(ip.is_global)


def _parse_ip_literal(host: str) -> Optional[str]:
    """``127.1``, ``2130706433``, ``0x7f.1`` gibi sayısal biçimler dahil IP'ye çevir; ad ise None."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None, flags=socket.AI_NUMERICHOST)
    except (socket.gaierror, UnicodeError, ValueError):
        return None
    return infos[0][4][0] if infos else None


def _resolve(host: str, port: int) -> list[str]:
    """Ana makine adını çöz (testlerde yamalanabilir)."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    seen: list[str] = []
    for info in infos:
        addr = info[4][0]
        if addr not in seen:
            seen.append(addr)
    return seen


# --------------------------------------------------------------------------
# URL doğrulama
# --------------------------------------------------------------------------
def _split(url: Any) -> SplitResult:
    if not isinstance(url, str) or not url:
        raise UnsafeURLError("URL boş ya da metin değil")
    if len(url) > MAX_URL_LENGTH:
        raise UnsafeURLError(f"URL çok uzun (en çok {MAX_URL_LENGTH} karakter)")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url) or "\\" in url:
        raise UnsafeURLError("URL boşluk, kontrol karakteri ya da ters bölü içeremez")
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeURLError("yalnız http ve https URL'lerine izin verilir")
    if "@" in parts.netloc:
        raise UnsafeURLError("URL içinde kullanıcı bilgisi (user@host) kullanılamaz")
    if not parts.hostname:
        raise UnsafeURLError("URL'de ana makine adı yok")
    try:
        parts.port  # geçersiz port ValueError fırlatır
    except ValueError as exc:
        raise UnsafeURLError("geçersiz port") from exc
    return parts


def _normalize_host(parts: SplitResult) -> tuple[str, bool]:
    host = parts.hostname or ""
    if parts.netloc.rpartition("]")[0].startswith("["):
        # Köşeli parantezli IPv6
        try:
            return str(ipaddress.IPv6Address(host.split("%", 1)[0])), True
        except ValueError as exc:
            raise UnsafeURLError("geçersiz IPv6 adresi") from exc
    literal = _parse_ip_literal(host)
    if literal is not None:
        return literal, True
    try:
        ascii_host = host.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise UnsafeURLError("geçersiz ana makine adı") from exc
    if not ascii_host or any(ch not in _HOST_CHARS for ch in ascii_host):
        raise UnsafeURLError("geçersiz ana makine adı")
    return ascii_host, False


def _check_name(host: str) -> None:
    if host in _BLOCKED_HOSTNAMES or host.endswith(_BLOCKED_SUFFIXES) or "." not in host:
        raise UnsafeURLError(f"iç ağ ana makine adına izin verilmiyor: {host}")


def _request_url(parts: SplitResult, host: str) -> str:
    """URL'yi denetlenen ana makine adıyla yeniden kur.

    curl ana makine adını kendi ayrıştırır; ``example.com.`` gibi bir yazım
    ``CURLOPT_RESOLVE`` kaydıyla (``example.com``) eşleşmezse adı kendisi yeniden
    çözer ve sabitleme boşa çıkar. Bu yüzden curl'e özgün yazım değil, tam olarak
    denetlenen ad verilir. Parça (``#...``) zaten sunucuya gitmez, atılır.
    """
    netloc = f"[{host}]" if ":" in host else host
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def check_url(url: Any, *, resolve: bool = True) -> Target:
    """URL'yi politikaya göre doğrula.

    ``resolve=False`` DNS sorgusu yapmaz (API girdi katmanı için hızlı ön kontrol);
    asıl yaptırım ağa çıkan katmanda ``resolve=True`` ile yapılır.
    """
    parts = _split(url)
    host, literal = _normalize_host(parts)
    port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
    req_url = _request_url(parts, host)

    if _allow_private():
        return Target(url=url, host=host, port=port, ip_literal=literal, pinned_ip=None, request_url=req_url)

    if literal:
        if not is_public_ip(host):
            raise UnsafeURLError(f"iç/özel ağ adresine izin verilmiyor: {host}")
        return Target(url=url, host=host, port=port, ip_literal=True, pinned_ip=host, request_url=req_url)

    _check_name(host)
    if not resolve:
        return Target(url=url, host=host, port=port, ip_literal=False, pinned_ip=None, request_url=req_url)

    try:
        addrs = _resolve(host, port)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise HostResolutionError(f"ana makine adı çözülemedi: {host}") from exc
    if not addrs:
        raise HostResolutionError(f"ana makine adı çözülemedi: {host}")
    bad = [a for a in addrs if not is_public_ip(a)]
    if bad:
        raise UnsafeURLError(f"{host} iç/özel ağ adresine çözülüyor: {bad[0]}")
    return Target(url=url, host=host, port=port, ip_literal=False, pinned_ip=addrs[0], request_url=req_url)


def check_api_url(url: Any) -> str:
    """API girdi doğrulaması (DNS'siz). Geçerliyse URL'yi aynen döndürür."""
    check_url(url, resolve=False)
    return url


# --------------------------------------------------------------------------
# Korumalı HTTP isteği (curl_cffi)
# --------------------------------------------------------------------------
def _pin_options(target: Target, using_proxy: bool) -> dict:
    opts: dict = {CurlOpt.PROTOCOLS_STR: "http,https"}
    if target.pinned_ip and not target.ip_literal and not using_proxy:
        ip = target.pinned_ip
        ip_fmt = f"[{ip}]" if ":" in ip else ip
        opts[CurlOpt.RESOLVE] = [f"{target.host}:{target.port}:{ip_fmt}"]
    return opts


def guarded_request(
    method: str,
    url: str,
    *,
    max_redirects: int = DEFAULT_MAX_REDIRECTS,
    proxies: Optional[dict] = None,
    request_func: Optional[Callable[..., Any]] = None,
    **kwargs: Any,
):
    """SSRF korumalı tek istek; yönlendirmeleri elle, her adımı doğrulayarak izler.

    Politika ihlalinde ``UnsafeURLError``, sınır aşımında ``TooManyRedirects``,
    çözümleme hatasında ``HostResolutionError`` fırlatır; son ikisi ve diğer ağ
    hataları curl_cffi'nin ``RequestsError`` türündendir.
    """
    send = request_func or curl_requests.request
    using_proxy = bool(proxies and any(proxies.values()))
    current_url, current_method = url, method.upper()
    body_kwargs = {k: kwargs.pop(k) for k in ("json", "data", "content") if k in kwargs}

    for hop in range(max_redirects + 1):
        target = check_url(current_url, resolve=True)
        resp = send(
            current_method,
            target.request_url,
            proxies=proxies,
            allow_redirects=False,
            curl_options=_pin_options(target, using_proxy),
            **body_kwargs,
            **kwargs,
        )
        primary_ip = getattr(resp, "primary_ip", "") or ""
        if primary_ip and not using_proxy and not _allow_private() and not is_public_ip(primary_ip):
            raise UnsafeURLError(f"bağlantı iç/özel ağ adresine gitti: {primary_ip}")

        location = resp.headers.get("location") if resp.status_code in _REDIRECT_CODES else None
        if not location:
            return resp
        if hop >= max_redirects:
            if max_redirects == 0:
                return resp  # yönlendirme izlenmiyor; 3xx yanıtını olduğu gibi döndür
            raise TooManyRedirects(f"yönlendirme sınırı aşıldı ({max_redirects}): {url}")
        next_url = urljoin(current_url, location)
        logger.debug("[netguard] yönlendirme %s -> %s", current_url, next_url)
        if resp.status_code == 303 or (resp.status_code in (301, 302) and current_method == "POST"):
            current_method, body_kwargs = "GET", {}
        current_url = next_url

    raise TooManyRedirects(f"yönlendirme sınırı aşıldı ({max_redirects}): {url}")  # pragma: no cover
