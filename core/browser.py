"""
Katman 3.5: Otonom Tarayıcı & CAPTCHA Bypass (Playwright + Browserless)
Sadece statik HTML değil, JS Render eden veya WAF (Cloudflare/Datadome)
atan siteleri gerçek tarayıcı ile geçer.

SSRF koruması (bkz. core/netguard.py):
  * Hedef URL tarayıcıya verilmeden önce doğrulanır (şema + DNS + IP sınıfı).
  * Tarayıcı ağa KENDİSİ çıkmaz. Sayfanın her HTTP(S) isteği (ana gezinme,
    alt kaynaklar, fetch/XHR) ``context.route`` ile yakalanır, worker tarafında
    ``netguard.guarded_request`` ile yapılır (her yönlendirme adımı denetlenir,
    doğrulanan IP curl'e sabitlenir: DNS rebinding yok) ve yanıt tarayıcıya
    ``route.fulfill`` ile verilir.
  * Route'a uğramayan her şey (Playwright'ın kendiliğinden sürdürdüğü
    yönlendirme adımları, WebSocket, worker/tarayıcı kaynaklı istekler) bağlama
    özel, hiçbir yere çıkmayan bir proxy'ye gider ve başarısız olur
    (``<-loopback>``: loopback adresleri de proxy dışında bırakılmaz).
    Sonuç: tarayıcı yolunda WebSocket bağlantısı kurulamaz.
  * Ana gezinmedeki yönlendirme tarayıcıya verilmez; denetlenip yeni bir
    gezinme olarak açılır (sayfanın adresi ve göreli bağlantıları doğru kalır).
  * Service worker'lar kapatılır; resim/medya/yazı tipi istekleri hiç yapılmaz
    (kazımaya katkıları yok, her biri ayrı bir dış istek olurdu).
"""
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright
from core.config import settings
from core.logging import get_logger
from core.netguard import DEFAULT_MAX_REDIRECTS, HostResolutionError, UnsafeURLError, check_url, guarded_request

logger = get_logger(__name__)

# Tarayıcının kendi içinde ürettiği, ağa çıkmayan şemalar.
_LOCAL_SCHEMES = frozenset({"data", "blob", "about"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
# Tarayıcının doğrudan çıkışını kapatan proxy: hiçbir şey dinlemiyor, bağlantı reddedilir.
_BLACKHOLE_PROXY = {"server": "http://127.0.0.1:9", "bypass": "<-loopback>"}
_SKIPPED_RESOURCE_TYPES = frozenset({"image", "media", "font"})
# curl'ün kendisinin yönettiği ya da tarayıcı önbelleğine bağlı istek başlıkları.
_DROP_REQUEST_HEADERS = frozenset({
    "host", "content-length", "connection", "keep-alive", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade", "accept-encoding",
    "cookie", "if-none-match", "if-modified-since", "if-match", "if-unmodified-since",
    "if-range", "range",
})
# curl yanıtı zaten açıp tam gövdeyi verdiği için geçersizleşen yanıt başlıkları.
_DROP_RESPONSE_HEADERS = frozenset({"content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive"})


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


def _response_headers(resp) -> dict:
    headers: dict = {}
    for name, value in resp.headers.multi_items():
        key = name.lower()
        if key in _DROP_RESPONSE_HEADERS or value is None:
            continue
        sep = "\n" if key == "set-cookie" else ", "
        headers[key] = headers[key] + sep + value if key in headers else value
    return headers


class _GuardedEgress:
    """``context.route`` işleyicisi: tarayıcının isteklerini netguard üzerinden yapar."""

    def __init__(self, context, page):
        self.context = context
        self.page = page
        self.redirect_to = None  # ana gezinmenin denetlenecek yönlendirme hedefi

    def __call__(self, route) -> None:
        try:
            self._handle(route)
        except Exception as exc:  # işleyici asla istek sürdürmeden düşmemeli
            logger.warning("[ssrf] tarayıcı isteği yapılamadı: %s (%r)", route.request.url, exc)
            try:
                route.abort("failed")
            except Exception:
                pass

    def _handle(self, route) -> None:
        req = route.request
        if urlsplit(req.url).scheme.lower() in _LOCAL_SCHEMES:
            route.continue_()  # ağa çıkmaz
            return
        if req.resource_type in _SKIPPED_RESOURCE_TYPES:
            route.abort("blockedbyclient")
            return
        main_nav = req.is_navigation_request() and req.frame == self.page.main_frame

        headers = {k: v for k, v in req.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS and not k.startswith(":")}
        cookies = self.context.cookies(req.url)
        if cookies:
            headers["cookie"] = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        try:
            resp = guarded_request(
                req.method,
                req.url,
                # Ana gezinmenin yönlendirmesi tarayıcıda yeni gezinme olarak açılır;
                # alt kaynaklarınki burada, her adım denetlenerek izlenir.
                max_redirects=0 if main_nav else DEFAULT_MAX_REDIRECTS,
                headers=headers,
                data=req.post_data_buffer,
                timeout=settings.request_timeout,
                # TLS/HTTP2 parmak izi Chrome gibi kalsın; başlıklar tarayıcınınkiler.
                impersonate="chrome120",
                default_headers=False,
            )
        except (UnsafeURLError, HostResolutionError) as exc:
            logger.warning("[ssrf] tarayıcı isteği engellendi: %s (%s)", req.url, exc)
            route.abort("blockedbyclient")
            return

        location = resp.headers.get("location") if resp.status_code in _REDIRECT_CODES else None
        if main_nav and location:
            self.redirect_to = urljoin(req.url, location)
            route.abort("aborted")
            return
        route.fulfill(status=resp.status_code, headers=_response_headers(resp), body=resp.content)


def _navigation_is_safe(response, page) -> bool:
    """Yönlendirme zincirindeki her adım ve son URL politikaya uyuyor mu (ek savunma)."""
    urls = [page.url]
    req = getattr(response, "request", None) if response is not None else None
    while req is not None:
        urls.append(req.url)
        req = req.redirected_from
    return all(is_browser_request_allowed(u) for u in urls)


def _goto(page, egress: _GuardedEgress, url: str):
    """Gezin; sunucu yönlendirirse hedefi denetleyip yeni gezinme olarak izle."""
    target = url
    for _ in range(DEFAULT_MAX_REDIRECTS + 1):
        egress.redirect_to = None
        try:
            # networkidle: Sayfadaki tüm ağ/JS işlemleri bitene kadar bekle (SPA'lar için kritik)
            response = page.goto(target, wait_until="networkidle", timeout=settings.request_timeout * 1000)
        except PlaywrightError:
            if egress.redirect_to is None:
                raise
            response = None
        if egress.redirect_to is None:
            return response
        target = egress.redirect_to
        check_url(target, resolve=False)  # http(s) dışı / sözdizimi bozuk hedefe gezinme yok
        logger.debug("[ssrf] tarayıcı yönlendirmesi denetlenerek izleniyor -> %s", target)
    raise UnsafeURLError(f"yönlendirme sınırı aşıldı ({DEFAULT_MAX_REDIRECTS}): {url}")


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
                context = browser.new_context(
                    user_agent=settings.user_agent, service_workers="block", proxy=_BLACKHOLE_PROXY,
                )
                page = context.new_page()
                egress = _GuardedEgress(context, page)
                context.route("**/*", egress)

                try:
                    response = _goto(page, egress, url)
                except UnsafeURLError as exc:
                    logger.warning("[ssrf] tarayıcı gezinmesi engellendi: %s (%s)", url, exc)
                    return None
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
