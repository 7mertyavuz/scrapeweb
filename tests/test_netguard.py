"""
SSRF savunması testleri (core/netguard.py ve onu kullanan fetcher, webhook, tarayıcı).

Ağa çıkılmaz: 127.0.0.1 üzerinde "iç" bir sunucu, 127.0.0.2 üzerinde de testte
"genel internet" sayılan bir sunucu açılır (netguard.is_public_ip yalnız 127.0.0.2
için yamalanır). Böylece "dış adres -> 302 -> iç adres" zinciri yerelde kurulur.
"""
from __future__ import annotations

import asyncio
import socketserver
import threading
import types
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import core.browser as browser_mod
import core.netguard as netguard
from core.config import settings
from core.fetcher import Fetcher

SECRET_HTML = """
<html><body><div class="product_main"><h1>GIZLI-IC-SAYFA</h1>
<p class="price_color">£1.00</p><p class="availability">In stock</p>
<p class="star-rating One"></p></div></body></html>
"""
FAKE_PUBLIC_IP = "127.0.0.2"


# --------------------------------------------------------------------------
# Yardımcılar
# --------------------------------------------------------------------------
class _QuickHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # HTTPServer.server_bind getfqdn() ile ters DNS sorgular (Windows'ta ~5 sn); atla.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class _Server:
    def __init__(self, host: str, routes: dict, port: int = 0):
        hits: list[tuple[str, str]] = []
        self.hits = hits

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                hits.append((self.command, self.path))
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                status, headers, body = routes.get(self.path, (404, {}, "yok"))
                data = body.encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _handle

            def log_message(self, *args):
                pass

        self.httpd = _QuickHTTPServer((host, port), Handler)
        self.host = host
        self.port = self.httpd.server_address[1]
        self.routes = routes
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str) -> str:
        return f"http://{self.host}:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def internal():
    srv = _Server("127.0.0.1", {"/secret": (200, {}, SECRET_HTML), "/hook": (200, {}, "ok")})
    yield srv
    srv.close()


@pytest.fixture
def public(monkeypatch):
    """127.0.0.2'yi 'genel internet' say; diğer her şey gerçek sınıflandırmayla."""
    real = netguard.is_public_ip
    monkeypatch.setattr(netguard, "is_public_ip", lambda ip: str(ip) == FAKE_PUBLIC_IP or real(ip))
    srv = _Server(FAKE_PUBLIC_IP, {"/page": (200, {}, "<html>genel-sayfa</html>")})
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _fast_fetcher(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_networks", False)
    monkeypatch.setattr(settings, "respect_robots", False)  # robots.txt için ağa çıkma
    monkeypatch.setattr(settings, "request_delay_sec", 0.0)
    monkeypatch.setattr(settings, "domain_delays", {})
    monkeypatch.setattr(settings, "max_retries", 1)
    monkeypatch.setattr(settings, "request_timeout", 5.0)
    monkeypatch.setattr(settings, "proxy_tier_1", "")
    monkeypatch.setattr(settings, "proxy_tier_2", "")
    monkeypatch.setattr(settings, "browser_egress_proxy", "")


# --------------------------------------------------------------------------
# IP / URL sınıflandırma (birim)
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ip", [
    "127.0.0.1", "127.5.5.5", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254",
    "100.64.0.1", "100.100.100.200", "0.0.0.0", "224.0.0.1", "255.255.255.255", "198.18.0.1",
    "::1", "::", "fe80::1", "fd00:ec2::254", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
    "2002:7f00:1::", "64:ff9b::7f00:1", "ff02::1",
])
def test_non_public_ips_rejected(ip):
    assert netguard.is_public_ip(ip) is False


@pytest.mark.parametrize("ip", ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111"])
def test_public_ips_accepted(ip):
    assert netguard.is_public_ip(ip) is True


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "gopher://example.com/", "ftp://example.com/", "dict://example.com/",
    "ws://example.com/", "javascript:alert(1)", "data:text/html,x", "http:///nohost",
    "http://u:p@example.com/", "http://example.com\\@127.0.0.1/", "http://exa mple.com/",
    "http://example.com:99999/", "http://example.com:0/", "https://[cdn]/x", "http://[::1/",
])
def test_syntax_and_scheme_rejected(url):
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url(url, resolve=False)


@pytest.mark.parametrize("url, port", [
    ("http://example.com/", 80), ("http://example.com:80/", 80), ("https://example.com:8443/x", 8443),
    ("http://93.184.216.34:81/", 81),
])
def test_checked_port_is_the_port_curl_connects_to(url, port):
    """Denetlenen/sabitlenen port ile curl'e giden URL'deki port aynı olmalı."""
    from urllib.parse import urlsplit

    t = netguard.check_url(url, resolve=False)
    assert t.port == port
    assert (urlsplit(t.request_url).port or (443 if t.request_url.startswith("https") else 80)) == port


def test_unparseable_redirect_location_is_policy_error(monkeypatch):
    """Ayrıştırılamayan Location (ör. https://[cdn]/x) çıplak ValueError değil UnsafeURLError'dır."""
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["93.184.216.34"])
    sent = []

    def fake_send(method, url, **kwargs):
        sent.append(url)
        return types.SimpleNamespace(status_code=302, headers={"location": "https://[cdn]/x"},
                                     primary_ip="93.184.216.34", text="")

    with pytest.raises(netguard.UnsafeURLError):
        netguard.guarded_request("GET", "https://example.com/", request_func=fake_send)
    assert sent == ["https://example.com/"]


def test_hostname_resolving_to_private_rejected(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["10.0.0.7"])
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("https://looks-public.example/", resolve=True)


def test_mixed_resolution_rejected(monkeypatch):
    """Adlardan biri bile iç adrese çözülüyorsa reddet (round-robin hilesi)."""
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["93.184.216.34", "127.0.0.1"])
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("https://mixed.example/", resolve=True)


def test_public_hostname_pinned(monkeypatch):
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["93.184.216.34"])
    t = netguard.check_url("https://Books.ToScrape.com/catalogue/x.html", resolve=True)
    assert t.host == "books.toscrape.com" and t.port == 443 and t.pinned_ip == "93.184.216.34"


def test_allow_private_networks_skips_ip_checks_only(monkeypatch):
    monkeypatch.setattr(settings, "allow_private_networks", True)
    assert netguard.check_url("http://127.0.0.1:8000/", resolve=True).host == "127.0.0.1"
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url("file:///etc/passwd")


def test_post_connect_ip_check(monkeypatch):
    """curl gerçekte iç adrese bağlandıysa (ayrıştırıcı farkı vb.) yanıt kullanılmaz."""
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["93.184.216.34"])

    def fake_send(method, url, **kwargs):
        return types.SimpleNamespace(status_code=200, headers={}, primary_ip="10.0.0.9", text="x")

    with pytest.raises(netguard.UnsafeURLError):
        netguard.guarded_request("GET", "https://example.com/", request_func=fake_send)


# --------------------------------------------------------------------------
# IP sabitleme: denetlenen ad == curl'ün bağlandığı adres (gerçek curl ile)
# --------------------------------------------------------------------------
@pytest.fixture
def twin_servers(monkeypatch):
    """Aynı portta iki dinleyici: 127.0.0.1 ("iç") ve 127.0.0.2 ("genel", sabitlenen IP).

    Denetim adı 127.0.0.2'ye çözer (rebinding'in ilk yanıtı gibi). curl adı kendisi
    yeniden çözerse 127.0.0.1'e gider; sabitleme tutarsa yalnız 127.0.0.2 isteği görür.
    """
    routes = {"/x": (200, {}, "ok")}
    inner = _Server("127.0.0.1", dict(routes))
    try:
        outer = _Server(FAKE_PUBLIC_IP, dict(routes), port=inner.port)
    except OSError:
        inner.close()
        pytest.skip("127.0.0.2 üzerinde aynı port açılamadı")
    real = netguard.is_public_ip
    monkeypatch.setattr(netguard, "is_public_ip", lambda ip: str(ip) == FAKE_PUBLIC_IP or real(ip))
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [FAKE_PUBLIC_IP])
    yield inner, outer
    inner.close()
    outer.close()


def _assert_pinned(inner, outer, method, url):
    kwargs = {"json": {"a": 1}} if method == "POST" else {}
    resp = netguard.guarded_request(method, url, timeout=5, max_redirects=0, **kwargs)
    assert inner.hits == [], "iç dinleyiciye (127.0.0.1) hiç istek gitmemeli"
    assert outer.hits == [(method, "/x")] and resp.status_code == 200


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("spelling", ["localhost", "localhost.", "LOCALHOST.", "LocalHost."])
def test_pinning_holds_for_every_host_spelling(twin_servers, monkeypatch, method, spelling):
    """Sonda nokta / büyük harf gibi yazımlar sabitlemeyi atlatmamalı.

    'localhost' burada saldırganın 127.0.0.1'e yeniden bağladığı bir adın yerine geçer
    (curl onu DNS'siz 127.0.0.1'e çözer); ad denetimi bu yüzden yalnız bu testte kapatılır.
    """
    inner, outer = twin_servers
    monkeypatch.setattr(netguard, "_check_name", lambda host: None)
    _assert_pinned(inner, outer, method, f"http://{spelling}:{inner.port}/x")


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pinning_holds_for_trailing_dot_real_dns(twin_servers, method):
    """Gözlenen açığın birebir hali: localtest.me (genel DNS'te 127.0.0.1) + sondaki nokta."""
    import socket

    try:
        resolved = {i[4][0] for i in socket.getaddrinfo("localtest.me", 80, type=socket.SOCK_STREAM)}
    except OSError:
        pytest.skip("DNS yok")
    if "127.0.0.1" not in resolved:
        pytest.skip("localtest.me 127.0.0.1'e çözülmüyor")
    inner, outer = twin_servers
    _assert_pinned(inner, outer, method, f"http://localtest.me.:{inner.port}/x")


# --------------------------------------------------------------------------
# Fetcher (worker'ın curl_cffi yolu) - gerçek yerel sunucularla
# --------------------------------------------------------------------------
def test_fetcher_blocks_loopback(internal):
    assert Fetcher().get(internal.url("/secret")) is None
    assert internal.hits == []


def test_fetcher_blocks_redirect_to_internal(internal, public):
    public.routes["/redir"] = (302, {"Location": internal.url("/secret")}, "")
    assert Fetcher().get(public.url("/redir")) is None
    assert public.hits == [("GET", "/redir")]
    assert internal.hits == [], "iç adrese hiç istek gitmemeli"


def test_fetcher_follows_safe_redirects(public):
    public.routes["/redir"] = (301, {"Location": "/page"}, "")
    assert Fetcher().get(public.url("/redir")) == "<html>genel-sayfa</html>"


def test_fetcher_redirect_limit(public):
    public.routes["/loop"] = (302, {"Location": "/loop"}, "")
    assert Fetcher().get(public.url("/loop")) is None
    assert len(public.hits) <= netguard.DEFAULT_MAX_REDIRECTS + 1


def test_fetcher_pins_validated_ip(public, monkeypatch):
    """Doğrulanan IP curl'e sabitlenir: curl kendi DNS'ine (rebinding) başvurmaz.

    'pinned.test' sistem DNS'inde yok; istek ancak sabitleme çalışıyorsa başarılı olur.
    """
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [FAKE_PUBLIC_IP])
    html = Fetcher().get(f"http://pinned.test:{public.port}/page")
    assert html == "<html>genel-sayfa</html>"


def test_fetcher_allows_private_with_escape_hatch(internal, monkeypatch):
    monkeypatch.setattr(settings, "allow_private_networks", True)
    assert "GIZLI-IC-SAYFA" in (Fetcher().get(internal.url("/secret")) or "")


def test_scrape_book_does_not_store_internal_page(internal, monkeypatch):
    """Uçtan uca: iç sayfa kitap olarak ayrıştırılıp DB'ye yazılmamalı (/books sızıntısı)."""
    import workers.tasks as tasks
    from core.dedup import Deduplicator
    from storage.db import count_books

    monkeypatch.setattr(tasks, "dedup", Deduplicator())
    with pytest.raises(tasks.FetchError):
        tasks.scrape_book.run(internal.url("/secret"))
    assert count_books() == 0
    assert internal.hits == []


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------
def test_webhook_blocks_internal_target(internal):
    import workers.tasks as tasks

    tasks.webhook_callback.run([{"status": "ok"}], internal.url("/hook"), {"x": 1})
    assert internal.hits == []


def test_webhook_does_not_follow_redirect_to_internal(internal, public):
    import workers.tasks as tasks

    public.routes["/hook"] = (307, {"Location": internal.url("/hook")}, "")
    tasks.webhook_callback.run([{"status": "ok"}], public.url("/hook"), {})
    assert public.hits == [("POST", "/hook")]
    assert internal.hits == []


def test_webhook_delivers_to_public_target(public):
    import workers.tasks as tasks

    public.routes["/hook"] = (200, {}, "ok")
    tasks.webhook_callback.run([{"status": "ok"}], public.url("/hook"), {})
    assert public.hits == [("POST", "/hook")]


def test_webhook_escape_hatch(internal, monkeypatch):
    import workers.tasks as tasks

    monkeypatch.setattr(settings, "allow_private_networks", True)
    tasks.webhook_callback.run([], internal.url("/hook"), {})
    assert internal.hits == [("POST", "/hook")]


# --------------------------------------------------------------------------
# Tarayıcı (AI) yolu - Playwright (async API) sahte nesnelerle
# --------------------------------------------------------------------------
class _Req:
    def __init__(self, url, prev=None):
        self.url, self.redirected_from = url, prev


class _FakePlaywright:
    """``async_playwright`` yerine: çağrıları, bağlam ayarlarını ve route'ları kaydeder."""

    def __init__(self, final_url, chain, content_delay=0.0):
        self.calls = []
        self.context_kwargs = {}
        self.routes = []
        outer = self

        class Page:
            url = final_url

            async def goto(self, url, **kw):
                outer.calls.append(("goto", url))
                prev = None
                for u in chain:
                    prev = _Req(u, prev)
                return types.SimpleNamespace(request=prev)

            async def content(self):
                await asyncio.sleep(content_delay)  # ana iş parçacığı kilitli sayfa benzetimi
                return "<html>tarayici-icerik</html>"

            async def wait_for_timeout(self, ms):
                pass

        class Context:
            async def route(self, pattern, handler):
                outer.routes.append(handler)

            async def new_page(self):
                return Page()

        class Browser:
            async def new_context(self, **kw):
                outer.context_kwargs = kw
                return Context()

            async def close(self):
                outer.calls.append(("close",))

        async def connect_over_cdp(url, **kw):
            return Browser()

        self.chromium = types.SimpleNamespace(connect_over_cdp=connect_over_cdp)

    def __call__(self):
        self.calls.append(("start",))
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def no_egress_proxy(monkeypatch):
    monkeypatch.setattr(settings, "browser_egress_proxy", "")


def test_browser_refuses_internal_target_before_connecting(monkeypatch):
    fake = _FakePlaywright("http://127.0.0.1/", ["http://127.0.0.1/"])
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    assert browser_mod.fetch_with_browser("http://127.0.0.1:8000/books") is None
    assert fake.calls == [], "iç adres tarayıcıya hiç verilmemeli"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://169.254.169.254/latest/meta-data/"])
def test_browser_refuses_unsafe_schemes_and_metadata(monkeypatch, url):
    fake = _FakePlaywright(url, [url])
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    assert browser_mod.fetch_with_browser(url) is None
    assert fake.calls == []


def test_browser_discards_content_after_redirect_to_internal(public, monkeypatch):
    start = public.url("/page")
    fake = _FakePlaywright("http://10.0.0.5/admin", [start, "http://10.0.0.5/admin"])
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    assert browser_mod.fetch_with_browser(start) is None
    assert ("close",) in fake.calls


def test_browser_sends_all_traffic_through_egress_proxy(public, monkeypatch):
    """Proxy ayarlıyken tarayıcı bağlamı tüm trafiğini denetimli çıkıştan geçirir (loopback dahil)."""
    monkeypatch.setattr(settings, "browser_egress_proxy", "http://egress:8899")
    start = public.url("/page")
    fake = _FakePlaywright(start, [start])
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    assert browser_mod.fetch_with_browser(start) == "<html>tarayici-icerik</html>"
    assert fake.context_kwargs.get("service_workers") == "block"
    assert fake.context_kwargs.get("proxy") == {"server": "http://egress:8899", "bypass": "<-loopback>"}
    assert fake.routes == [], "ağ katmanı denetlerken istek yakalanmaz"
    assert fake.calls[-1] == ("close",)


def test_browser_without_proxy_falls_back_to_url_filter(public, monkeypatch, no_egress_proxy):
    start = public.url("/page")
    fake = _FakePlaywright(start, [start])
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    assert browser_mod.fetch_with_browser(start) == "<html>tarayici-icerik</html>"
    assert "proxy" not in fake.context_kwargs
    assert fake.routes == [browser_mod._guard_route]


class _FakeRoute:
    def __init__(self, url):
        self.decision = None
        self.request = types.SimpleNamespace(url=url)

    async def continue_(self):
        self.decision = ("continue",)

    async def abort(self, reason=None):
        self.decision = ("abort", reason)


def test_browser_fallback_filter_decisions(internal, public):
    cases = {
        internal.url("/secret"): ("abort", "blockedbyclient"),
        "http://169.254.169.254/latest/meta-data/": ("abort", "blockedbyclient"),
        "http://localhost:9222/json": ("abort", "blockedbyclient"),
        public.url("/page"): ("continue",),
        "data:text/plain,x": ("continue",),
    }
    for url, expected in cases.items():
        route = _FakeRoute(url)
        asyncio.run(browser_mod._guard_route(route))
        assert route.decision == expected, url
    assert internal.hits == []


def test_browser_session_has_hard_deadline(public, monkeypatch):
    """Ana iş parçacığını kilitleyen sayfada page.content() asılı kalır; oturum yine süre sınırında biter."""
    import time

    monkeypatch.setattr(settings, "request_timeout", 0.2)
    start = public.url("/page")
    fake = _FakePlaywright(start, [start], content_delay=3600)
    monkeypatch.setattr(browser_mod, "async_playwright", fake)
    t0 = time.monotonic()
    assert browser_mod.fetch_with_browser(start) is None
    assert time.monotonic() - t0 < 0.2 * browser_mod._DEADLINE_FACTOR + 2
    assert fake.calls[-1] == ("close",), "süre dolunca bağlam/bağlantı kapatılmalı"


def test_browser_request_allowed_helper(public):
    assert browser_mod.is_browser_request_allowed("ws://127.0.0.1:9222/devtools") is False
    assert browser_mod.is_browser_request_allowed("data:image/png;base64,AA==") is True
    assert browser_mod.is_browser_request_allowed(public.url("/x.js")) is True


def test_unresolvable_host_is_transient_error_and_never_requested(monkeypatch):
    import socket

    def boom(host, port):
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(netguard, "_resolve", boom)
    with pytest.raises(netguard.HostResolutionError):
        netguard.check_url("https://does-not-exist.example/")
    sent = []
    with pytest.raises(netguard.HostResolutionError):
        netguard.guarded_request("GET", "https://does-not-exist.example/", request_func=lambda *a, **k: sent.append(a))
    assert sent == []
    assert Fetcher().get("https://does-not-exist.example/") is None
    assert browser_mod.is_browser_request_allowed("https://does-not-exist.example/x.js") is False
