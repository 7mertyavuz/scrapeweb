"""
SSRF savunması testleri (core/netguard.py ve onu kullanan fetcher, webhook, tarayıcı).

Ağa çıkılmaz: 127.0.0.1 üzerinde "iç" bir sunucu, 127.0.0.2 üzerinde de testte
"genel internet" sayılan bir sunucu açılır (netguard.is_public_ip yalnız 127.0.0.2
için yamalanır). Böylece "dış adres -> 302 -> iç adres" zinciri yerelde kurulur.
"""
from __future__ import annotations

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
    "http://example.com:99999/",
])
def test_syntax_and_scheme_rejected(url):
    with pytest.raises(netguard.UnsafeURLError):
        netguard.check_url(url, resolve=False)


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
# Tarayıcı (AI) yolu - Playwright sahte nesnelerle
# --------------------------------------------------------------------------
class _Req:
    def __init__(self, url, prev=None):
        self.url, self.redirected_from = url, prev


class _FakePlaywright:
    def __init__(self, final_url, chain):
        self.calls = []
        self.context_kwargs = {}
        self.routes = []
        outer = self

        class Page:
            url = final_url

            def goto(self, url, **kw):
                outer.calls.append(("goto", url))
                prev = None
                for u in chain:
                    prev = _Req(u, prev)
                return types.SimpleNamespace(request=prev)

            def content(self):
                return "<html>tarayici-icerik</html>"

            def wait_for_timeout(self, ms):
                pass

        class Context:
            def route(self, pattern, handler):
                outer.routes.append(handler)

            def new_page(self):
                return Page()

        class Browser:
            def new_context(self, **kw):
                outer.context_kwargs = kw
                return Context()

            def close(self):
                outer.calls.append(("close",))

        self.chromium = types.SimpleNamespace(connect_over_cdp=lambda url: Browser())

    def __call__(self):
        self.calls.append(("start",))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_browser_refuses_internal_target_before_connecting(monkeypatch):
    fake = _FakePlaywright("http://127.0.0.1/", ["http://127.0.0.1/"])
    monkeypatch.setattr(browser_mod, "sync_playwright", fake)
    assert browser_mod.fetch_with_browser("http://127.0.0.1:8000/books") is None
    assert fake.calls == [], "iç adres tarayıcıya hiç verilmemeli"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://169.254.169.254/latest/meta-data/"])
def test_browser_refuses_unsafe_schemes_and_metadata(monkeypatch, url):
    fake = _FakePlaywright(url, [url])
    monkeypatch.setattr(browser_mod, "sync_playwright", fake)
    assert browser_mod.fetch_with_browser(url) is None
    assert fake.calls == []


def test_browser_discards_content_after_redirect_to_internal(public, monkeypatch):
    start = public.url("/page")
    fake = _FakePlaywright("http://10.0.0.5/admin", [start, "http://10.0.0.5/admin"])
    monkeypatch.setattr(browser_mod, "sync_playwright", fake)
    assert browser_mod.fetch_with_browser(start) is None
    assert ("close",) in fake.calls


def test_browser_installs_request_guard_and_returns_safe_content(public, monkeypatch):
    start = public.url("/page")
    fake = _FakePlaywright(start, [start])
    monkeypatch.setattr(browser_mod, "sync_playwright", fake)
    assert browser_mod.fetch_with_browser(start) == "<html>tarayici-icerik</html>"
    assert fake.context_kwargs.get("service_workers") == "block"
    # Tarayıcının doğrudan çıkışı kapalı: route'a uğramayan her şey ölü proxy'ye gider,
    # loopback de proxy dışında bırakılmaz.
    assert fake.context_kwargs.get("proxy") == browser_mod._BLACKHOLE_PROXY
    assert "<-loopback>" in browser_mod._BLACKHOLE_PROXY["bypass"]
    assert len(fake.routes) == 1 and isinstance(fake.routes[0], browser_mod._GuardedEgress)


class _FakeRoute:
    """Playwright Route yerine: tarayıcının yaptığı bir istek ve ona verilen karar."""

    def __init__(self, url, *, method="GET", resource_type="document", navigation=False, frame="sub"):
        self.decision = None
        main = object()
        self.page = types.SimpleNamespace(main_frame=main)
        self.request = types.SimpleNamespace(
            url=url, method=method, resource_type=resource_type, headers={"user-agent": "UA", "host": "x"},
            post_data_buffer=None, frame=main if frame == "main" else object(),
            is_navigation_request=lambda: navigation,
        )

    def continue_(self):
        self.decision = ("continue",)

    def abort(self, reason=None):
        self.decision = ("abort", reason)

    def fulfill(self, status=None, headers=None, body=None):
        self.decision = ("fulfill", status, body)


def _egress_for(route):
    ctx = types.SimpleNamespace(cookies=lambda url: [])
    return browser_mod._GuardedEgress(ctx, route.page)


def test_browser_egress_fetches_through_netguard(internal, public):
    """Tarayıcı isteği worker'da netguard ile yapılır; yönlendirme adımları da denetlenir."""
    public.routes["/to-internal"] = (302, {"Location": internal.url("/secret")}, "")
    public.routes["/to-page"] = (302, {"Location": "/page"}, "")
    cases = [
        _FakeRoute(internal.url("/secret"), resource_type="fetch"),
        _FakeRoute("http://169.254.169.254/latest/meta-data/", resource_type="script"),
        _FakeRoute(public.url("/to-internal"), resource_type="script"),   # alt kaynak: 302 -> iç
        _FakeRoute(public.url("/to-internal"), navigation=True),          # iframe gezinmesi: 302 -> iç
        _FakeRoute(public.url("/to-page"), resource_type="xhr"),          # güvenli yönlendirme izlenir
        _FakeRoute(public.url("/page"), resource_type="stylesheet"),
        _FakeRoute(public.url("/page"), resource_type="image"),           # hiç istenmez
        _FakeRoute("data:text/plain,x", resource_type="fetch"),
    ]
    for route in cases:
        _egress_for(route)(route)
    decisions = [r.decision for r in cases]
    assert decisions == [
        ("abort", "blockedbyclient"),
        ("abort", "blockedbyclient"),
        ("abort", "blockedbyclient"),
        ("abort", "blockedbyclient"),
        ("fulfill", 200, b"<html>genel-sayfa</html>"),
        ("fulfill", 200, b"<html>genel-sayfa</html>"),
        ("abort", "blockedbyclient"),
        ("continue",),
    ]
    assert internal.hits == [], "iç adrese hiç istek gitmemeli"
    assert public.hits.count(("GET", "/page")) == 2


def test_browser_main_navigation_redirect_becomes_checked_navigation(internal, public):
    """Ana gezinmenin yönlendirmesi tarayıcıya verilmez; hedef kaydedilip yeni gezinme olur."""
    public.routes["/to-internal"] = (302, {"Location": internal.url("/secret")}, "")
    route = _FakeRoute(public.url("/to-internal"), navigation=True, frame="main")
    egress = _egress_for(route)
    egress(route)
    assert route.decision == ("abort", "aborted")
    assert egress.redirect_to == internal.url("/secret")
    assert internal.hits == []


def test_browser_goto_follows_redirect_only_after_check(monkeypatch):
    from playwright.sync_api import Error as PlaywrightError

    egress = browser_mod._GuardedEgress(None, None)
    plan = {"http://a.example/": "http://10.0.0.5/admin"}
    visited = []

    class Page:
        def goto(self, url, **kw):
            visited.append(url)
            if url in plan:
                egress.redirect_to = plan[url]
                raise PlaywrightError("net::ERR_ABORTED")
            return "resp"

    with pytest.raises(netguard.UnsafeURLError):
        browser_mod._goto(Page(), egress, "http://a.example/")
    assert visited == ["http://a.example/"], "iç hedefe gezinme hiç başlatılmamalı"

    plan = {"http://a.example/": "http://a.example/b", "http://a.example/b": "http://c.example/"}
    assert browser_mod._goto(Page(), egress, "http://a.example/") == "resp"
    assert visited[1:] == ["http://a.example/", "http://a.example/b", "http://c.example/"]


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
