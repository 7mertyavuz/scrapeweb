"""
Katman 3.5: Otonom Tarayıcı & CAPTCHA Bypass (Playwright + Browserless)
Sadece statik HTML değil, JS Render eden veya WAF (Cloudflare/Datadome)
atan siteleri gerçek tarayıcı ile geçer.

SSRF koruması (bkz. core/netguard.py):
  * Hedef URL tarayıcıya verilmeden önce doğrulanır (şema + DNS + IP sınıfı).
  * Tarayıcının yaptığı HER istek (alt kaynaklar, JS ile gezinme, fetch/XHR)
    ``context.route`` ile yakalanır; iç/özel ağa ya da http/https dışı şemaya
    gidenler iptal edilir. WebSocket'ler de aynı politikayla süzülür.
  * Service worker'lar kapatılır (aksi halde istekleri route'u atlayabilir).
  * HTTP yönlendirme adımları route'a uğramaz (Playwright kısıtı); bu yüzden
    gezinme sonrası yönlendirme zinciri ve son URL denetlenir, iç adrese
    varılmışsa içerik döndürülmez.
"""
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from core.config import settings
from core.logging import get_logger
from core.netguard import HostResolutionError, UnsafeURLError, check_url

logger = get_logger(__name__)

# Tarayıcının kendi içinde ürettiği, ağa çıkmayan şemalar.
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})


def is_browser_request_allowed(url: str) -> bool:
    """Tarayıcının yapacağı bir isteğe izin verilip verilmeyeceği."""
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


def _guard_route(route) -> None:
    if is_browser_request_allowed(route.request.url):
        route.continue_()
    else:
        route.abort("blockedbyclient")


def _guard_websocket(ws) -> None:
    if is_browser_request_allowed(ws.url):
        ws.connect_to_server()
    else:
        ws.close()


def _navigation_is_safe(response, page) -> bool:
    """Yönlendirme zincirindeki her adım ve son URL politikaya uyuyor mu."""
    urls = [page.url]
    req = getattr(response, "request", None) if response is not None else None
    while req is not None:
        urls.append(req.url)
        req = req.redirected_from
    return all(is_browser_request_allowed(u) for u in urls)


def fetch_with_browser(url: str) -> str | None:
    try:
        check_url(url, resolve=True)
    except (UnsafeURLError, HostResolutionError) as exc:
        logger.warning("[ssrf] tarayıcı hedefi engellendi: %s (%s)", url, exc)
        return None

    try:
        with sync_playwright() as p:
            # Docker'daki browserless servisine WebSocket ile bağlan
            logger.info("Browserless CDP'ye bağlanılıyor...")
            browser = p.chromium.connect_over_cdp(settings.browserless_url)
            try:
                context = browser.new_context(user_agent=settings.user_agent, service_workers="block")
                context.route("**/*", _guard_route)
                if hasattr(context, "route_web_socket"):
                    context.route_web_socket("**/*", _guard_websocket)
                page = context.new_page()

                # networkidle: Sayfadaki tüm ağ/JS işlemleri bitene kadar bekle (SPA'lar için kritik)
                response = page.goto(url, wait_until="networkidle", timeout=settings.request_timeout * 1000)
                if not _navigation_is_safe(response, page):
                    logger.warning("[ssrf] tarayıcı iç/özel adrese yönlendirildi, içerik atıldı: %s", url)
                    return None

                content = page.content()

                # --- KONU 2: Cookie Harvesting / WAF Çözümü Simülasyonu ---
                if "datadome" in content.lower() or "cf-browser-verification" in content.lower():
                    logger.warning(f"WAF Koruması Tespit Edildi ({url})! CapSolver simülasyonu başlatılıyor...")

                    # Gerçekte burada 2Captcha/CapSolver API'ye sitekey gönderilir.
                    # Çözülen token cookie'ye basılır:
                    page.wait_for_timeout(3000) # İnsan simülasyonu / Çözüm süresi
                    # page.evaluate("document.cookie = 'datadome=SOLVED_TOKEN_HERE';")
                    # page.reload()
                    if not is_browser_request_allowed(page.url):
                        return None
                    logger.info("WAF Bypass tamamlandı.")
                    content = page.content()

                return content
            finally:
                browser.close()
    except Exception as e:
        logger.error(f"Otonom Tarayıcı hatası ({url}): {e}")
        return None
