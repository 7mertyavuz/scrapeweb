"""
Tarayıcı çıkış proxy'si (core/egress_proxy.py) testleri.

Ağa çıkılmaz: proxy bir arka plan iş parçacığında 127.0.0.1'de çalışır. 127.0.0.1'deki
sunucu "iç", 127.0.0.2'deki sunucu testte "genel internet" sayılır (netguard.is_public_ip
yalnız 127.0.0.2 için yamalanır; proxy de aynı işlevi kullanır).
"""
from __future__ import annotations

import asyncio
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import core.egress_proxy as egress
import core.netguard as netguard
from core.config import settings

FAKE_PUBLIC_IP = "127.0.0.2"
_END = b"\r\n\r\n"


class _QuickHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # getfqdn() atla (Windows'ta yavaş)
        self.server_name, self.server_port = self.server_address[:2]


class _Server:
    def __init__(self, host: str, port: int = 0, body: str = "genel-sayfa"):
        hits: list[tuple[str, str, str]] = []
        self.hits = hits

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append((self.command, self.path, self.headers.get("Host", "")))
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.httpd = _QuickHTTPServer((host, port), Handler)
        self.host, self.port = host, self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def proxy(monkeypatch):
    """Proxy'yi ayrı bir olay döngüsünde başlat; (host, port) döndür."""
    monkeypatch.setattr(settings, "allow_private_networks", False)
    real = netguard.is_public_ip
    fake_public = lambda ip: str(ip) == FAKE_PUBLIC_IP or real(ip)
    monkeypatch.setattr(netguard, "is_public_ip", fake_public)
    monkeypatch.setattr(egress, "is_public_ip", fake_public)

    loop = asyncio.new_event_loop()
    started = threading.Event()
    box = {}

    async def start():
        slots = asyncio.Semaphore(egress.MAX_CLIENTS)
        box["server"] = await asyncio.start_server(
            lambda r, w: egress._serve_proxy(r, w, slots), "127.0.0.1", 0, limit=egress.MAX_HEAD_BYTES
        )
        started.set()

    def run():
        loop.run_until_complete(start())
        loop.run_forever()

    threading.Thread(target=run, daemon=True).start()
    assert started.wait(5)
    yield "127.0.0.1", box["server"].sockets[0].getsockname()[1]
    loop.call_soon_threadsafe(box["server"].close)
    loop.call_soon_threadsafe(loop.stop)


@pytest.fixture
def internal():
    srv = _Server("127.0.0.1", body="GIZLI-IC-SAYFA")
    yield srv
    srv.close()


@pytest.fixture
def public():
    srv = _Server(FAKE_PUBLIC_IP)
    yield srv
    srv.close()


def _read_response(s: socket.socket, buf: bytes = b"") -> tuple[bytes, bytes]:
    """Bir HTTP yanıtı oku (başlık + Content-Length kadar gövde); (yanıt, artan) döndür.

    Hedef sunucu bağlantıyı açık tutsa da (keep-alive) gövdenin sonu, gerçek tarayıcıdaki
    gibi Content-Length'ten anlaşılır.
    """
    while _END not in buf:
        chunk = s.recv(4096)
        if not chunk:
            return buf, b""
        buf += chunk
    head, _, rest = buf.partition(_END)
    length = 0
    for line in head.split(_END[:2])[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value)
    while len(rest) < length:
        chunk = s.recv(65536)
        if not chunk:
            break
        rest += chunk
    return head + _END + rest[:length], rest[length:]


def _send(proxy_addr, raw: bytes, then: bytes = b"") -> bytes:
    """Proxy'ye ham istek gönder ve yanıtı oku; CONNECT ise tünel kurulunca ``then`` gönderilir."""
    with socket.create_connection(proxy_addr, timeout=5) as s:
        s.sendall(raw)
        resp, rest = _read_response(s)
        if not then or not resp.startswith(b"HTTP/1.1 200"):
            return resp
        s.sendall(then)
        inner, _ = _read_response(s, rest)
        return resp + inner


def _get(url: str) -> bytes:
    return f"GET {url} HTTP/1.1\r\nHost: ignored.example\r\nProxy-Connection: keep-alive\r\n\r\n".encode()


def _connect(authority: str) -> bytes:
    return f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode()


_TUNNELED_GET = b"GET /page HTTP/1.1\r\nHost: t\r\nConnection: close\r\n\r\n"


# --------------------------------------------------------------------------
# İç/özel hedefler: hiçbir bağlantı açılmaz
# --------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["http", "connect"])
def test_internal_target_is_refused(proxy, internal, kind):
    authority = f"127.0.0.1:{internal.port}"
    raw = _get(f"http://{authority}/secret") if kind == "http" else _connect(authority)
    resp = _send(proxy, raw, then=_TUNNELED_GET if kind == "connect" else b"")
    assert resp.startswith(b"HTTP/1.1 403")
    assert internal.hits == [], "iç dinleyiciye hiçbir şey ulaşmamalı"


@pytest.mark.parametrize("authority", [
    "localhost:80", "egress:8899", "browserless:3000", "169.254.169.254:80", "[::1]:80",
    "10.0.0.5:443", "2130706433:80", "0x7f.1:80",
])
def test_internal_names_and_spellings_are_refused(proxy, authority):
    assert _send(proxy, _connect(authority)).startswith(b"HTTP/1.1 403")
    assert _send(proxy, _get(f"http://{authority}/")).startswith(b"HTTP/1.1 403")


def test_name_resolving_to_internal_is_refused(proxy, internal, monkeypatch):
    """Bölünmüş DNS: başka yerde genel görünen ad burada iç adrese çözülüyorsa reddedilir."""
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: ["127.0.0.1"])
    assert _send(proxy, _get(f"http://looks-public.example:{internal.port}/")).startswith(b"HTTP/1.1 403")
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: [FAKE_PUBLIC_IP, "127.0.0.1"])
    assert _send(proxy, _connect(f"mixed.example:{internal.port}")).startswith(b"HTTP/1.1 403")
    assert internal.hits == []


def test_malformed_requests_are_rejected(proxy):
    assert _send(proxy, b"GET / HTTP/1.1\r\nHost: x\r\n\r\n").startswith(b"HTTP/1.1 400")      # origin-form
    assert _send(proxy, _get("ftp://example.com/")).startswith(b"HTTP/1.1 400")
    assert _send(proxy, _get("http://[cdn]/x")).startswith(b"HTTP/1.1 400")
    assert _send(proxy, b"garbage\r\n\r\n").startswith(b"HTTP/1.1 400")


# --------------------------------------------------------------------------
# Genel hedefler: aktarılır
# --------------------------------------------------------------------------
def test_plain_http_is_relayed_with_checked_host(proxy, public):
    resp = _send(proxy, _get(f"http://{FAKE_PUBLIC_IP}:{public.port}/page?q=1"))
    head, _, body = resp.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.0 200") or head.startswith(b"HTTP/1.1 200")
    assert body == b"genel-sayfa"
    assert b"Connection: close" in head and b"keep-alive" not in head.lower()
    # Yol olduğu gibi, Host başlığı istemcinin yazdığı değil denetlenen hedef.
    assert public.hits == [("GET", "/page?q=1", f"{FAKE_PUBLIC_IP}:{public.port}")]


def test_connect_tunnel_to_public(proxy, public):
    resp = _send(proxy, _connect(f"{FAKE_PUBLIC_IP}:{public.port}"), then=_TUNNELED_GET)
    assert resp.startswith(b"HTTP/1.1 200 Connection Established")
    assert resp.endswith(b"genel-sayfa")
    assert public.hits == [("GET", "/page", "t")]


# --------------------------------------------------------------------------
# DNS rebinding: bağlantı DENETLENEN adrese açılır, ad yeniden çözülmez
# --------------------------------------------------------------------------
@pytest.fixture
def twin(monkeypatch):
    """Aynı portta 127.0.0.1 ("iç") ve 127.0.0.2 ("genel"); ad denetimde 127.0.0.2'ye çözülür."""
    inner = _Server("127.0.0.1", body="GIZLI-IC-SAYFA")
    try:
        outer = _Server(FAKE_PUBLIC_IP, port=inner.port)
    except OSError:
        inner.close()
        pytest.skip("127.0.0.2 üzerinde aynı port açılamadı")
    answers = iter([[FAKE_PUBLIC_IP]] + [["127.0.0.1"]] * 10)   # ilk yanıt genel, sonra iç
    monkeypatch.setattr(netguard, "_resolve", lambda host, port: next(answers))
    yield inner, outer
    inner.close()
    outer.close()


@pytest.mark.parametrize("kind", ["http", "connect"])
def test_connection_goes_to_the_checked_ip(proxy, twin, kind):
    inner, outer = twin
    authority = f"rebind.example:{inner.port}"
    raw = _get(f"http://{authority}/page") if kind == "http" else _connect(authority)
    resp = _send(proxy, raw, then=_TUNNELED_GET if kind == "connect" else b"")
    assert resp.endswith(b"genel-sayfa")
    assert inner.hits == [], "ad yeniden çözülüp iç adrese bağlanılmamalı"
    assert len(outer.hits) == 1


# --------------------------------------------------------------------------
# Kaçış kapısı ve TCP aktarımı
# --------------------------------------------------------------------------
def test_allow_private_networks_escape_hatch(proxy, internal, monkeypatch):
    monkeypatch.setattr(settings, "allow_private_networks", True)
    resp = _send(proxy, _get(f"http://127.0.0.1:{internal.port}/secret"))
    assert resp.endswith(b"GIZLI-IC-SAYFA")


def test_parse_forward_spec():
    assert egress._parse_forward("3000=browserless:3000") == (3000, "browserless", 3000)


# --------------------------------------------------------------------------
# Gövdeli düz HTTP istekleri: gövde, yanıt beklenmeden hedefe aktarılır
# --------------------------------------------------------------------------
class _EchoServer(_Server):
    """POST gövdesini (Content-Length ya da chunked) okuyup aynen geri yollar."""

    def __init__(self, host: str):
        received: list[bytes] = []
        self.received = received

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    data = b""
                    while size := int(self.rfile.readline().split(b";")[0], 16):
                        data += self.rfile.read(size)
                        self.rfile.readline()
                    self.rfile.readline()
                else:
                    data = self.rfile.read(int(self.headers["Content-Length"]))
                received.append(data)
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.hits = []
        self.httpd = _QuickHTTPServer((host, 0), Handler)
        self.host, self.port = host, self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def echo():
    srv = _EchoServer(FAKE_PUBLIC_IP)
    yield srv
    srv.close()


@pytest.mark.parametrize("framing", ["content-length", "chunked"])
def test_plain_http_request_body_is_forwarded(proxy, echo, framing):
    import time

    body = b"ad=deneme&not=" + b"x" * 5000
    if framing == "chunked":
        framed, header = b"%x\r\n%s\r\n0\r\n\r\n" % (len(body), body), "Transfer-Encoding: chunked"
    else:
        framed, header = body, f"Content-Length: {len(body)}"
    raw = (f"POST http://{FAKE_PUBLIC_IP}:{echo.port}/post HTTP/1.1\r\nHost: x\r\n{header}\r\n"
           "Content-Type: application/x-www-form-urlencoded\r\n\r\n").encode() + framed
    t0 = time.monotonic()
    resp = _send(proxy, raw)
    assert resp.startswith(b"HTTP/1.1 200") and resp.endswith(body)
    assert echo.received == [body]
    assert time.monotonic() - t0 < 2


# --------------------------------------------------------------------------
# Bağlantı yerleri: istemcisi gitmiş ya da boşta kalan bağlantı yer tutmaz
# --------------------------------------------------------------------------
class _RawServer:
    """Her bağlantıyı ``serve(conn)`` ile karşılayan çıplak TCP sunucusu (varsayılan: hiç yanıt vermez)."""

    def __init__(self, host: str, serve=None):
        self.sock = socket.create_server((host, 0))
        self.host, self.port = host, self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        self._serve = serve or (lambda conn: None)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def close(self):
        self.sock.close()
        for conn in self.conns:
            conn.close()


@pytest.fixture
def silent():
    srv = _RawServer(FAKE_PUBLIC_IP)
    yield srv
    srv.close()


@pytest.fixture
def few_slots(monkeypatch):
    """``proxy`` fikstüründen ÖNCE istenmeli: semafor bu değerle kurulur."""
    monkeypatch.setattr(egress, "MAX_CLIENTS", 4)


def _wait_until(predicate, timeout: float = 3.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("kind", ["http", "connect"])
def test_client_that_left_frees_its_slot(few_slots, proxy, silent, public, kind):
    """Yanıt vermeyen hedefe istek atıp giden istemciler proxy'yi başkalarına kapatmaz."""
    authority = f"{FAKE_PUBLIC_IP}:{silent.port}"
    for _ in range(egress.MAX_CLIENTS):
        with socket.create_connection(proxy, timeout=5) as s:
            if kind == "http":
                s.sendall(_get(f"http://{authority}/asili"))
            else:
                s.sendall(_connect(authority))
                assert s.recv(4096).startswith(b"HTTP/1.1 200")
            assert _wait_until(lambda: len(silent.conns) > 0)
    page = _get(f"http://{FAKE_PUBLIC_IP}:{public.port}/page")
    assert _wait_until(lambda: _send(proxy, page).endswith(b"genel-sayfa")), "yerler serbest kalmalı"


@pytest.mark.parametrize("kind", ["http", "connect"])
def test_idle_connection_is_closed(proxy, silent, monkeypatch, kind):
    """İstemci bağlı kalsa da iki yönde veri akmayan bağlantı IDLE_TIMEOUT sonunda kapanır."""
    import time

    monkeypatch.setattr(egress, "IDLE_TIMEOUT", 0.3)
    authority = f"{FAKE_PUBLIC_IP}:{silent.port}"
    with socket.create_connection(proxy, timeout=5) as s:
        t0 = time.monotonic()
        if kind == "http":
            s.sendall(_get(f"http://{authority}/asili"))
        else:
            s.sendall(_connect(authority))
            assert s.recv(4096).startswith(b"HTTP/1.1 200")
        rest = b""
        while chunk := s.recv(4096):   # proxy kapatana kadar (socket zaman aşımı 5 sn)
            rest += chunk
        assert time.monotonic() - t0 < 2
        assert rest == b""


def test_active_slow_stream_is_not_cut(proxy, monkeypatch):
    """Veri akan bağlantı, toplam süresi IDLE_TIMEOUT'u aşsa da kesilmez."""
    import time

    monkeypatch.setattr(egress, "IDLE_TIMEOUT", 0.3)
    parts = 8

    def trickle(conn):
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % parts)
        for _ in range(parts):
            time.sleep(0.15)   # her parça arası < IDLE_TIMEOUT, toplam > IDLE_TIMEOUT
            conn.sendall(b"x")
        conn.close()

    srv = _RawServer(FAKE_PUBLIC_IP, serve=trickle)
    try:
        resp = _send(proxy, _get(f"http://{FAKE_PUBLIC_IP}:{srv.port}/akan"))
    finally:
        srv.close()
    assert resp.startswith(b"HTTP/1.1 200") and resp.endswith(b"x" * parts)
