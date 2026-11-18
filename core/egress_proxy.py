"""
Tarayıcının (browserless) internete tek çıkış noktası: SSRF denetimli HTTP proxy'si.

Compose'da browserless yalnız ``internal: true`` olan ``browser`` ağındadır: internete,
host'a ve diğer ağlara yolu yoktur. Bu süreç hem o ağda hem internete açık ``outbound``
ağındadır; tarayıcı bağlamı (core/browser.py) bütün trafiğini buraya yollar:

* HTTPS ve WebSocket (ws/wss): ``CONNECT ad:port`` tüneli;
* düz HTTP: ``GET http://ad/yol`` biçiminde istek (bağlantı başına bir istek).

Her bağlantıda hedef ``netguard.check_url`` ile denetlenir (ad çözülür; çözülen
adreslerden biri bile iç/özel ise red) ve TCP bağlantısı DENETLENEN IP'ye açılır; böylece
denetim ile bağlantı arasında DNS yeniden bağlama (rebinding) olamaz. Bağlandıktan sonra
karşı uç bir kez daha denetlenir. Yönlendirmeleri, alt kaynakları, fetch/XHR'ı ve
WebSocket'i tarayıcı kendisi yapar; her biri ayrı bir bağlantı olarak buradan geçer ve
ayrı ayrı denetlenir. Proxy kullanan tarayıcı adları kendisi çözmez, adı proxy çözer.
HTTPS içeriği uçtan uca şifreli kalır; proxy yalnız hedefi (ad:port) görür.

Çalıştırma:  python -m core.egress_proxy [--port 8899] [--forward 3000=browserless:3000]

``--forward``: politika uygulamayan düz TCP aktarımı. ``internal`` ağdaki bir konteynerin
portu host'a yayımlanamadığı için compose, browserless'ın 127.0.0.1:3000 portunu bu
servis üzerinden yayımlar (browserless yine token ister).
"""
from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlsplit

from core.config import settings
from core.logging import get_logger
from core.netguard import HostResolutionError, UnsafeURLError, check_url, is_public_ip

logger = get_logger("core.egress_proxy")

DEFAULT_PORT = 8899
MAX_HEAD_BYTES = 64 * 1024     # istek/yanıt başlık bloğu üst sınırı
HEAD_TIMEOUT = 30              # istemcinin başlıkları göndermesi için süre (sn)
CONNECT_TIMEOUT = 10           # hedefe TCP bağlantısı için süre (sn)
MAX_TUNNEL_SECONDS = 15 * 60   # tek bir bağlantının en uzun ömrü (sızıntıya karşı)
MAX_CLIENTS = 512              # eşzamanlı bağlantı sınırı

# Bağlantıya özgü (hop-by-hop) başlıklar: iletilmez; proxy kendi değerini koyar.
_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate",
    "te", "trailer", "upgrade",
})
_CRLF = "\r\n"


def _allow_private() -> bool:
    return bool(settings.allow_private_networks)


def _strip_headers(lines: list[str], drop: frozenset) -> list[str]:
    return [ln for ln in lines if ln and ln.split(":", 1)[0].strip().lower() not in drop]


async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    writer.write(f"HTTP/1.1 {status} {reason}{_CRLF}Content-Length: 0{_CRLF}Connection: close{_CRLF}{_CRLF}".encode())
    try:
        await writer.drain()
    except OSError:
        pass


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except OSError:
        pass


async def _relay(c_reader, c_writer, u_reader, u_writer) -> None:
    """İki yönü aktar; bir taraf kapanınca (ya da süre dolunca) ikisini de bırak."""
    tasks = [asyncio.create_task(_pipe(c_reader, u_writer)), asyncio.create_task(_pipe(u_reader, c_writer))]
    _, pending = await asyncio.wait(tasks, timeout=MAX_TUNNEL_SECONDS, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)


async def _open_checked(url: str):
    """Hedefi denetle ve DENETLENEN adrese bağlan.

    ``((target, reader, writer), None)`` ya da ``(None, (durum kodu, gerekçe))`` döndürür.
    """
    try:
        target = await asyncio.to_thread(check_url, url, resolve=True)
    except UnsafeURLError as exc:
        logger.warning("[ssrf] tarayıcı çıkışı engellendi: %s (%s)", url, exc)
        return None, (403, "Forbidden")
    except HostResolutionError as exc:
        logger.info("[egress] çözülemedi: %s (%s)", url, exc)
        return None, (502, "Bad Gateway")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target.pinned_ip or target.host, target.port), CONNECT_TIMEOUT
        )
    except (OSError, asyncio.TimeoutError) as exc:
        logger.info("[egress] bağlanılamadı: %s (%r)", url, exc)
        return None, (502, "Bad Gateway")
    peer = writer.get_extra_info("peername")
    if peer and not _allow_private() and not is_public_ip(peer[0]):
        writer.close()
        logger.warning("[ssrf] bağlantı iç/özel adrese gitti, kesildi: %s -> %s", url, peer[0])
        return None, (403, "Forbidden")
    return (target, reader, writer), None


async def _handle(c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter) -> None:
    try:
        head = await asyncio.wait_for(c_reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
        return
    lines = head.decode("latin-1").split(_CRLF)
    try:
        method, target, version = lines[0].split(" ")
    except ValueError:
        return await _reply(c_writer, 400, "Bad Request")

    if method.upper() == "CONNECT":
        # HTTPS ve WebSocket tünelleri. Şema yalnız biçimsel; bağlanılan port denetlenen porttur.
        opened, error = await _open_checked(f"https://{target}/")
        if error:
            return await _reply(c_writer, *error)
        _, u_reader, u_writer = opened
        c_writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    else:
        # Düz HTTP: "GET http://ad[:port]/yol HTTP/1.1". Denetlenen şey köken (ad + port);
        # yol olduğu gibi, Host başlığı denetlenen adla gider.
        try:
            parts = urlsplit(target)
        except ValueError:
            parts = None
        if parts is None or parts.scheme.lower() != "http":
            return await _reply(c_writer, 400, "Bad Request")
        opened, error = await _open_checked(f"http://{parts.netloc}/")
        if error:
            return await _reply(c_writer, *error)
        checked, u_reader, u_writer = opened
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        headers = _strip_headers(lines[1:], _HOP_HEADERS | {"host"})
        request = [f"{method} {path} {version}", f"Host: {urlsplit(checked.request_url).netloc}", *headers]
        u_writer.write(_CRLF.join([*request, "Connection: close", "", ""]).encode("latin-1"))
        try:
            resp_head = await asyncio.wait_for(u_reader.readuntil(b"\r\n\r\n"), MAX_TUNNEL_SECONDS)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError):
            u_writer.close()
            return await _reply(c_writer, 502, "Bad Gateway")
        # Bağlantı başına tek istek: tarayıcı bu bağlantıyı başka bir hedef için yeniden kullanmasın.
        resp_lines = resp_head.decode("latin-1").split(_CRLF)
        kept = _strip_headers(resp_lines[1:], _HOP_HEADERS)
        c_writer.write(_CRLF.join(
            [resp_lines[0], *kept, "Connection: close", "Proxy-Connection: close", "", ""]
        ).encode("latin-1"))

    try:
        await _relay(c_reader, c_writer, u_reader, u_writer)
    finally:
        u_writer.close()


async def _serve_proxy(c_reader, c_writer, slots: asyncio.Semaphore) -> None:
    try:
        if slots.locked():
            return await _reply(c_writer, 503, "Service Unavailable")
        async with slots:
            await _handle(c_reader, c_writer)
    except Exception as exc:  # tek bağlantının hatası süreci düşürmemeli
        logger.warning("[egress] bağlantı hatası: %r", exc)
    finally:
        c_writer.close()


async def _serve_forward(c_reader, c_writer, host: str, port: int) -> None:
    try:
        u_reader, u_writer = await asyncio.wait_for(asyncio.open_connection(host, port), CONNECT_TIMEOUT)
    except (OSError, asyncio.TimeoutError):
        c_writer.close()
        return
    try:
        await _relay(c_reader, c_writer, u_reader, u_writer)
    finally:
        u_writer.close()
        c_writer.close()


async def serve(port: int = DEFAULT_PORT, forwards: tuple[tuple[int, str, int], ...] = (),
                host: str = "0.0.0.0") -> None:
    slots = asyncio.Semaphore(MAX_CLIENTS)
    servers = [await asyncio.start_server(
        lambda r, w: _serve_proxy(r, w, slots), host, port, limit=MAX_HEAD_BYTES
    )]
    for listen, dest_host, dest_port in forwards:
        servers.append(await asyncio.start_server(
            lambda r, w, h=dest_host, d=dest_port: _serve_forward(r, w, h, d), host, listen
        ))
    logger.info(
        "[egress] proxy :%d dinlemede (iç/özel ağ %s)%s", port,
        "SERBEST (ALLOW_PRIVATE_NETWORKS=true)" if _allow_private() else "engelli",
        "".join(f"; :{lp} -> {h}:{d}" for lp, h, d in forwards),
    )
    await asyncio.gather(*(s.serve_forever() for s in servers))


def _parse_forward(spec: str) -> tuple[int, str, int]:
    listen, _, dest = spec.partition("=")
    dest_host, _, dest_port = dest.rpartition(":")
    return int(listen), dest_host, int(dest_port)


def main() -> None:
    ap = argparse.ArgumentParser(description="ScrapeHub tarayıcı çıkış proxy'si (SSRF denetimli)")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--forward", action="append", default=[], metavar="PORT=HOST:PORT")
    args = ap.parse_args()
    asyncio.run(serve(args.port, tuple(_parse_forward(f) for f in args.forward)))


if __name__ == "__main__":
    main()
