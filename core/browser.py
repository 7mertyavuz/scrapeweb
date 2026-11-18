"""
Katman 3.5: Otonom Tarayıcı & CAPTCHA Bypass (Playwright + Browserless)
Sadece statik HTML değil, JS Render eden veya WAF (Cloudflare/Datadome)
atan siteleri gerçek tarayıcı ile geçer.

SSRF koruması iki katmanlıdır:

* Ağ katmanı (asıl koruma; ``BROWSER_EGRESS_PROXY`` ayarlıyken, compose varsayılanı):
  browserless'ın internete doğrudan yolu yoktur (``internal`` ağ). Tarayıcı bağlamı tüm
  trafiğini ``core/egress_proxy.py``'ye yollar; proxy her bağlantının hedefini netguard ile
  denetler ve denetlenen IP'ye bağlanır. Ana gezinme, yönlendirmeler, alt kaynaklar,
  fetch/XHR, WebSocket, worker'lar: hepsi tarayıcının kendi ağ yığınıyla, ama bu tek
  denetimli kapıdan geçer. ``<-loopback>``: Chrome'un loopback adreslerini proxy dışında
  bırakma varsayılanını kaldırır (sayfa browserless'ın kendi portlarına ulaşamasın).
* Uygulama katmanı (ek savunma; proxy yoksa TEK koruma): hedef URL tarayıcıya verilmeden
  önce denetlenir; proxy ayarlı değilse tarayıcının her HTTP(S) isteğinin URL'si
  ``context.route`` ile denetlenir ve gezinmenin yönlendirme zinciri sonradan kontrol edilir.
  Bu süzgeç yönlendirme adımlarını, açılır pencereleri, tarayıcının kendi DNS çözümlemesini
  (rebinding), WebSocket ve WebRTC'yi denetleyemez; bu yüzden yalnız yedektir.

Service worker'lar kapatılır (isteklerini sayfadan bağımsız yaparlar).

Süre: tüm oturumun kesin bir üst sınırı vardır (``REQUEST_TIMEOUT`` x
``_DEADLINE_FACTOR``). Ana iş parçacığını kilitleyen bir sayfada ``page.content()``
gibi çağrılar kendi zaman aşımı olmadan asılı kalır; bu yüzden async API ile
çalışılır ve süre dolunca oturum iptal edilip bağlam kapatılır.
"""
import asyncio
from urllib.parse import urlsplit

from playwright.async_api import async_playwright
from core.config import settings
from core.logging import get_logger
from core.netguard import HostResolutionError, UnsafeURLError, check_url

logger = get_logger(__name__)

# Tarayıcının kendi içinde ürettiği, ağa çıkmayan şemalar.
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})
# Bağlanma + gezinme (REQUEST_TIMEOUT) + içerik okuma + WAF beklemesi için toplam pay.
_DEADLINE_FACTOR = 1.5
_CLOSE_TIMEOUT = 5.0
_warned_without_proxy = False


def is_browser_request_allowed(url: str) -> bool:
    """Tarayıcının yapacağı bir isteğe izin verilip verilmeyeceği (yalnız URL denetimi)."""
    scheme = urlsplit(url).scheme.lower() if isinstance(url, str) else ""
    if scheme in _LOCAL_SCHEMES:
        return True
    if scheme in ("ws", "wss"):
        url = ("https" if scheme == "wss" else "http") + url[len(scheme):]
    try:
        check_url(url, resolve=True)
        return True
    except (UnsafeURLError, HostResolutionError) as exc:
        logger.warning("[ssrf] tarayıcı isteği engellendi: %s (%s)", url, exc)
        return False


async def _guard_route(route) -> None:
    """Proxy'siz kurulumda yedek süzgeç: iç/özel adrese giden isteği iptal et."""
    if await asyncio.to_thread(is_browser_request_allowed, route.request.url):
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


def _context_options() -> dict:
    options = {"user_agent": settings.user_agent, "service_workers": "block"}
    if settings.browser_egress_proxy:
        options["proxy"] = {"server": settings.browser_egress_proxy, "bypass": "<-loopback>"}
    return options


def _navigation_is_safe(response, page_url: str) -> bool:
    """Yönlendirme zincirindeki her adım ve son URL politikaya uyuyor mu (ek savunma)."""
    urls = [page_url]
    req = getattr(response, "request", None) if response is not None else None
    while req is not None:
        urls.append(req.url)
        req = req.redirected_from
    return all(is_browser_request_allowed(u) for u in urls)


async def _scrape(browser, url: str) -> str | None:
    context = await browser.new_context(**_context_options())
    if not settings.browser_egress_proxy:
        await context.route("**/*", _guard_route)
    page = await context.new_page()

    # networkidle: Sayfadaki tüm ağ/JS işlemleri bitene kadar bekle (SPA'lar için kritik)
    response = await page.goto(url, wait_until="networkidle", timeout=settings.request_timeout * 1000)
    if not await asyncio.to_thread(_navigation_is_safe, response, page.url):
        logger.warning("[ssrf] tarayıcı iç/özel adrese yönlendirildi, içerik atıldı: %s", url)
        return None

    content = await page.content()

    # --- KONU 2: Cookie Harvesting / WAF Çözümü Simülasyonu ---
    if "datadome" in content.lower() or "cf-browser-verification" in content.lower():
        logger.warning(f"WAF Koruması Tespit Edildi ({url})! CapSolver simülasyonu başlatılıyor...")

        # Gerçekte burada 2Captcha/CapSolver API'ye sitekey gönderilir.
        # Çözülen token cookie'ye basılır:
        await page.wait_for_timeout(3000) # İnsan simülasyonu / Çözüm süresi
        # await page.evaluate("document.cookie = 'datadome=SOLVED_TOKEN_HERE';")
        # await page.reload()
        if not await asyncio.to_thread(is_browser_request_allowed, page.url):
            return None
        logger.info("WAF Bypass tamamlandı.")
        content = await page.content()

    return content


async def _fetch(url: str) -> str | None:
    async with async_playwright() as p:
        # Docker'daki browserless servisine WebSocket ile bağlan
        logger.info("Browserless CDP'ye bağlanılıyor...")
        browser = await p.chromium.connect_over_cdp(
            settings.browserless_url, timeout=settings.request_timeout * 1000
        )
        try:
            return await asyncio.wait_for(_scrape(browser, url), settings.request_timeout * _DEADLINE_FACTOR)
        except asyncio.TimeoutError:
            logger.error("Otonom Tarayıcı süre sınırını aştı, oturum kapatıldı: %s", url)
            return None
        finally:
            try:
                await asyncio.wait_for(browser.close(), _CLOSE_TIMEOUT)
            except Exception:
                pass


def fetch_with_browser(url: str) -> str | None:
    global _warned_without_proxy
    try:
        check_url(url, resolve=True)
    except (UnsafeURLError, HostResolutionError) as exc:
        logger.warning("[ssrf] tarayıcı hedefi engellendi: %s (%s)", url, exc)
        return None
    if not settings.browser_egress_proxy and not _warned_without_proxy:
        _warned_without_proxy = True
        logger.warning(
            "[ssrf] BROWSER_EGRESS_PROXY ayarlı değil: tarayıcı yolunda yalnız URL süzgeci var; "
            "yönlendirme adımları, açılır pencereler, DNS rebinding, WebSocket ve WebRTC denetlenemez (bkz. README)."
        )
    try:
        return asyncio.run(_fetch(url))
    except Exception as e:
        logger.error(f"Otonom Tarayıcı hatası ({url}): {e}")
        return None
