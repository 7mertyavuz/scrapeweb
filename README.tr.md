# ScrapeHub — AI Destekli Web Scraping Pipeline'ı

[![CI](https://github.com/7mertyavuz/websc/actions/workflows/ci.yml/badge.svg)](https://github.com/7mertyavuz/websc/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**[English](README.md) | [Türkçe](README.tr.md)**

**FastAPI**, **Celery**, **Redis**, **PostgreSQL** ve **OpenTelemetry** ile kurulmuş, üretime hazır, yatayda ölçeklenebilir bir web scraping pipeline'ıdır. Katalog tarama, tekilleştirme, dayanıklı fetch, çok kademeli parse ve kalıcı depolama işlemlerini yönetir. İsteğe bağlı olarak Playwright + Ollama ile desteklenen AI destekli modu da bulunur.

> **Durum:** Bu proje eğitim amaçlı olarak `books.toscrape.com` üzerinde çalışır, ancak altyapı kurumsal düzeyde scraping iş yükleri için tasarlanmıştır.
>
> 📖 **Mimariyi derinlemesine incelemek için → [DOKUMANTASYON.md](DOKUMANTASYON.md)**

---

## ScrapeHub Nedir?

ScrapeHub, bir web sitesini yapılandırılmış veriye dönüştüren ve veritabanına yazan dağıtık bir web scraping pipeline'ıdır. Beş bağımsız katman üzerine kuruludur:

1. **Orkestratör** — İstekleri alan ve işleri kuyruğa koyan FastAPI ağ geçidi.
2. **Kuyruk** — Redis destekli Celery broker'ı ile görev dağıtımı.
3. **Tekilleştirme** — Gereksiz işi önleyen Bloom filter + içerik hash'i.
4. **Fetch & Parse** — Stealth için `curl_cffi`, JS ağırlıklı sayfalar için Playwright ve dayanıklı fallback parser.
5. **Depolama** — Atomik upsert destekli SQLAlchemy + PostgreSQL/SQLite.

## Öne Çıkan Özellikler

- **Mikroservis odaklı:** API, worker'lar, Redis, Postgres ve Flower ayrı servisler olarak çalışır.
- **Yatay ölçeklenebilir:** Daha fazla Celery worker ekleyerek throughput artırılır.
- **Dayanıklı fetch:** Üstel backoff, jitter ve domain bazlı nezaket gecikmesiyle yeniden deneme.
- **Tekilleştirme:** RedisBloom varsa kullanır, yoksa bellek içi fallback'e düşer. Bir URL ancak kitabı kaydedildikten sonra "görüldü" sayılır ve en az `DEDUP_TTL` (varsayılan 24 sa) öyle kalır; kayıtlar tek tek sona erer.
- **Artımlı mod:** Sayfa içeriği değiştiğinde yeniden işler.
- **AI destekli mod:** Browserless Chrome + Ollama LLM ile dinamik kazıma.
- **Dead-letter kuyruğu:** Başarısız görevler sonradan incelenmek üzere yakalanır.
- **Gözlemlenebilirlik:** Prometheus metrikleri, Flower görev izleme ve Jaeger dağıtık izleme.
- **Web dashboard:** `http://localhost:8000` adresinde sunulan karanlık tema kontrol paneli.

## Hızlı Başlangıç

### Seçenek A — Sıfır altyapı (yerel SQLite)

```bash
pip install -r requirements.txt
python run_local.py --pages 2
```

### Seçenek B — Tam dağıtık mod (Docker)

Önce `.env` dosyasını oluşturup zorunlu sırları doldurun (`API_KEY`, `REDIS_PASSWORD`, `POSTGRES_PASSWORD`, `BROWSERLESS_TOKEN`, `FLOWER_PASSWORD`). Parolalarda yalnız harf ve rakam kullanın: bağlantı URL'lerinin içine yazılırlar. Biri eksikse `docker compose` hata verip durur.

```bash
cp .env.example .env      # Windows: copy .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # her sır için bir rastgele değer
docker compose up --build
```

Ardından bir kazıma işi başlatın (`$API_KEY`, `.env`'e yazdığınız değerdir):

```bash
curl -X POST http://localhost:8000/scrape \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"max_pages": 5, "chunk_size": 10}'
```

İlerlemeyi kontrol edin:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:8000/stats
curl http://localhost:8000/health
```

Arayüzleri açın:

- **Web Arayüzü:** http://localhost:8000 (API anahtarını sol menüye girin)
- **Flower:** http://localhost:5555 (giriş: `FLOWER_USER`, varsayılan `admin` / `FLOWER_PASSWORD`)
- **Metrikler:** http://localhost:8000/metrics
- **Jaeger:** http://localhost:16686

## API Uç Noktaları

Tüm `POST` uç noktaları `X-API-Key` header'ı gerektirir. `GET /status/{task_id}`, `/stats`, `/books` ve `/dead-letter` da `PUBLIC_READ=true` değilse anahtar ister. `/`, `/static/*`, `/health` ve `/metrics` açıktır. Eksik ya da yanlış anahtar `401` döner.

| Uç Nokta | Metot | Açıklama |
|----------|-------|----------|
| `/` | GET | Web arayüzü (`index.html`). |
| `/scrape` | POST | Katalog tarama (standart mod). |
| `/scrape-dynamic` | POST | Katalog tarama (AI / Playwright modu). |
| `/scrape-one` | POST | Tek URL kazıma; `force=true` dedup'u atlar. |
| `/status/{task_id}` | GET | Celery görev durumu. |
| `/stats` | GET | Kitap sayısı + tekilleştirme backend bilgisi. |
| `/books` | GET | Veritabanındaki kitaplar (sayfalı). |
| `/dead-letter` | GET | İnceleme için yakalanan başarısız görevler, en yeni önce (son 1000 kayıt tutulur). |
| `/health` | GET | Derin sağlık kontrolü: Redis'e doğrudan ping (kısa zaman aşımı) + PostgreSQL; biri başarısızsa `503`. |
| `/metrics` | GET | Prometheus metrik endpoint'i. |

İstek sınırları: `max_pages` 1–`MAX_PAGES_LIMIT` (varsayılan 100), `chunk_size` 1–100 arasıdır; `start_url`, `webhook_url` ve `url` en çok 2048 karakterlik, genel (iç ağ olmayan) `http`/`https` adresleri olmalıdır. Aksi `422` ile reddedilir.

## Konfigürasyon

Önemli ortam değişkenleri (tam liste için `core/config.py`):

| Değişken | Default | Açıklama |
|----------|---------|----------|
| `SCRAPE_BASE_URL` | `https://books.toscrape.com/` | Hedef site |
| `REDIS_URL` | `redis://localhost:6379/0` | Broker + Bloom filter |
| `DATABASE_URL` | `sqlite:///scrapehub.db` | Üretimde PostgreSQL önerilir |
| `API_KEY` | — (zorunlu) | FastAPI endpoint koruması; ayarlı değilse korumalı her istek `401` alır |
| `PUBLIC_READ` | `false` | `true` → `GET` veri uçları anahtarsız okunabilir |
| `CORS_ORIGINS` | (boş) | Virgülle ayrılmış izinli origin'ler; boş = çapraz origin kapalı |
| `ALLOW_PRIVATE_NETWORKS` | `false` | `true` → özel/iç ağ hedeflerine izin (yalnız yerel geliştirme) |
| `MAX_PAGES_LIMIT` | `100` | API'nin kabul ettiği en büyük `max_pages` |
| `REQUEST_DELAY` | `0.5` | İstekler arası nezaket gecikmesi |
| `MAX_CONCURRENCY` | `4` | Worker eşzamanlılık tabanı |
| `MAX_RETRIES` | `3` | Başarısız fetch için yeniden deneme |
| `INCREMENTAL` | `false` | Sadece içerik değiştiğinde yeniden işle |
| `DEDUP_TTL` | `86400` | Kaydedilen bir URL'nin "görüldü" kaldığı saniye (Redis ve bellek içi) |
| `BROWSERLESS_URL` | `ws://localhost:3000` | AI modu için headless Chrome |
| `BROWSER_EGRESS_PROXY` | (boş; compose: `http://egress:8899`) | Tarayıcının tüm trafiğini gönderdiği SSRF denetimli proxy, browserless'ın gördüğü adresle; boş = yalnız uygulama içi URL süzgeci |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | Yerel LLM endpoint'i |
| `JAEGER_ENDPOINT` | `http://localhost:4318/v1/traces` | OpenTelemetry trace'leri |

## Test

```bash
pip install -r requirements-dev.txt
pytest -q
```

Uygulamayı çalıştırmak için `requirements.txt` yeterlidir; `requirements-dev.txt` test araçlarını ekler. `tests/conftest.py` testleri PostgreSQL'e yönlendirir (`DATABASE_URL`, varsayılan `postgresql+psycopg2://scrape:scrape@localhost:5432/scrapehub`), her testten önce tabloları sıfırlar, bellek içi tekilleştirmeyi zorlar ve teste özgü bir `API_KEY` ayarlar.

## Mimari

```
  ┌──────────────┐   POST /scrape    ┌──────────────────┐
  │   İstemci /   │ ───────────────► │  FastAPI (API)   │   Katman 1: Orkestratör
  │   Cron job    │  (X-API-Key)     │   app/main.py    │
  └──────────────┘                  └────────┬─────────┘
                                              │ .delay()
                                     ┌────────▼─────────┐
                                     │  Redis (broker)  │   Katman 1: Kuyruk
                                     └────────┬─────────┘
                                              │
                              ┌───────────────▼───────────────┐
                              │   Celery Workers (xN)         │
                              │   workers/tasks.py            │
                              │                               │
                              │  1. Dedup  (Bloom Filter)     │◄─ Katman 2
                              │  2. Fetch  (curl_cffi /       │◄─ Katman 3+4
                              │            Playwright + LLM)  │
                              │  3. Parse  (JSON-LD/CSS/regex)│◄─ Katman 5
                              │  4. Store  (ON CONFLICT)      │
                              └───────────────┬───────────────┘
                                              │
                                     ┌────────▼──────────┐
                                     │  PostgreSQL /     │   Depolama
                                     │  SQLite           │
                                     └───────────────────┘
```

### Servis Haritası (Docker Compose)

| Servis | Amaç | Port |
|--------|------|------|
| `api` | FastAPI ağ geçidi + statik dashboard | `8000` |
| `worker` | Celery görev worker'ları | — |
| `redis` | Broker + Bloom filter + dead-letter | `6379`, `8001` |
| `postgres` | Kalıcı ilişkisel depolama | `5432` |
| `flower` | Celery izleme arayüzü | `5555` |
| `browserless` | Dinamik/AI modu için headless Chrome | `3000` (`egress` yayımlar) |
| `egress` | Tarayıcının tek çıkışı: SSRF denetimli proxy (`core/egress_proxy.py`) | — |
| `jaeger` | Dağıtık izleme arayüzü | `16686` |

Tüm portlar yalnız `127.0.0.1`'e yayınlanır. API'yi ağa açmak için `.env` içinde `API_BIND_ADDR=0.0.0.0` yapın.

## Güvenlik

- **API anahtarı:** `API_KEY`'in varsayılanı yoktur. Yazma uçları ve `PUBLIC_READ=true` değilse veri okuma uçları `X-API-Key` ister; `/`, `/static/*`, `/health` ve `/metrics` açık kalır.
- **Hedefler:** Kazıma hedefleri ve `webhook_url` yalnız genel `http`/`https` adresleri olabilir; loopback, özel ağ, link-local (bulut metadata) ve benzeri adresler reddedilir. Ana makine adı bağlanmadan önce çözülüp denetlenir, denetlenen IP sabitlenir (curl'e denetlenen adla yeniden kurulmuş URL verilir; sonda nokta gibi yazımlar sabitlemeyi atlatamaz) ve her yönlendirme yeniden denetlenir (webhook yönlendirme izlemez). `ALLOW_PRIVATE_NETWORKS=true` yerel geliştirme için adres denetimlerini kapatır.
- **Tarayıcı/AI modu:** Compose kurulumunda browserless'ın internete ve diğer servislere yolu yoktur. Host'tan tamamen kopuk değildir: `browser` ağının Docker host'unda yine bir ağ geçidi adresi vardır ve Docker Desktop'ta browserless'tan bu adrese TCP bağlantısı reddedildi (yani orada yanıt veren bir ağ yığını var); Linux'ta host'ta `0.0.0.0`'a bağlı servislere bu yoldan ulaşılıp ulaşılamadığı denenmedi. İnternete tek çıkışı `egress` proxy'sidir: tarayıcının açtığı her bağlantı (sayfa, yönlendirmeler, alt kaynaklar, fetch/XHR, WebSocket) ondan geçer; her hedef kazıma hedefleriyle aynı denetimden geçer ve bağlantı denetlenen IP'ye açılır. Bir tarayıcı oturumu, browserless'a bağlanma dahil, 1,5 × `REQUEST_TIMEOUT` sonunda kesilir; proxy bir bağlantıyı istemcisi gidince hemen, iki yönde de 60 sn veri akmazsa da kapatır.
- **CORS**, `CORS_ORIGINS` origin listelemedikçe kapalıdır.
- **Docker varsayılanları:** Portlar `127.0.0.1`'e bağlı; Redis, Postgres, browserless ve Flower `.env`'deki parola/token'ı ister; imaj root olmayan kullanıcıyla çalışır. browserless yalnız worker ve `egress` ile paylaştığı `internal` bir `browser` ağında çalışır; Redis, Postgres, Jaeger ve Flower'a ulaşamaz (host için yukarıdaki Tarayıcı/AI modu maddesine bakın).
- **Panel:** Sunucu verisi HTML olarak değil, düz metin olarak basılır.

Bilinen sınırlar:

- Verilen compose dosyası dışında (`BROWSER_EGRESS_PROXY` boş) tarayıcı yolunda yalnız uygulama içi bir URL süzgeci vardır: her HTTP(S) isteğinin URL'sini ve gezinmenin yönlendirme zincirini denetler; yönlendirme adımlarını (denemede iç dinleyiciye kör istek olarak ulaştılar; içerikleri dönmedi), açılır pencereleri, tarayıcının kendi DNS çözümlemesini (rebinding), WebSocket ve WebRTC'yi denetleyemez. Tam koruma için browserless'ı tek çıkışı `python -m core.egress_proxy` olacak şekilde kurun (compose dosyasındaki gibi) ve `BROWSER_EGRESS_PROXY` verin.
- WebRTC'nin UDP'si proxy'den geçmez. Compose dosyası onu `DEFAULT_LAUNCH_ARGS=["--webrtc-ip-handling-policy=disable_non_proxied_udp"]` ile kapatır; bu bayrak olmadan sayfa `browser` ağındaki diğer konteynerlere (worker, `egress`) yine UDP paketi gönderebilir. browserless'ı kendiniz çalıştırıyorsanız aynı bayrağı verin.
- Tarayıcı yolunda DNS rebinding: proxy her adı bir kez çözer ve denetlenen IP'ye bağlanır; bu bölünmüş DNS ile denendi, gerçek bir rebinding DNS sunucusuyla denenmedi.
- Proxy (`PROXY_TIER_1`/`PROXY_TIER_2`) kullanılırken IP sabitleme ve bağlanılan IP denetimi uygulanmaz.
- Hız sınırı (rate limiting) yoktur.
- `/docs` (OpenAPI arayüzü) açıktır.

**Yükseltme:** Eski sürümler belgelerde sabit bir varsayılan API anahtarı yayımlıyordu. Bu anahtar artık her zaman reddedilir; onunla kurulum yaptıysanız yeni, rastgele bir `API_KEY` ayarlayın. Eski sürümler tekilleştirmeyi ömürsüz tek bir `scrapehub:seen_urls` anahtarında tutuyordu; yeni kod ona bakmaz, `docker compose exec redis redis-cli DEL scrapehub:seen_urls` ile silebilirsiniz.

## Etik & Yasal Uyarı

Bu proje, izin veren hedeflerde eğitim amaçlı kullanım içindir. `core/fetcher.py`, `robots.txt`'e saygı duyar ve nezaket gecikmesi uygular. Başka domain'leri kazırken hedef sitenin Kullanım Koşulları ve ilgili veri gizliliği yasalarına (GDPR/KVKK) uyma sorumluluğu tamamen kullanıcıya aittir.
