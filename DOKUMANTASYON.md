# ScrapeHub — Ayrıntılı Dokümantasyon ve Mimari Rehberi

> Bu dosya, projeyi **sıfırdan** anlamak isteyen biri için yazılmış kapsamlı bir rehberdir.
> Hiç scraping/Celery/Redis bilmeden okumaya başlayabilirsin; her kavramı yeri geldikçe
> açıklıyoruz. Hızlı kurulum için ana [README.md](README.md)'ye bak; bu dosya **"neden ve
> nasıl"** sorularına odaklanır.

İçindekiler:

1. [Proje ne yapıyor?](#1-proje-ne-yapıyor)
2. [Büyük resim: dağıtık pipeline nedir?](#2-büyük-resim-dağıtık-pipeline-nedir)
3. [Bir isteğin baştan sona yolculuğu](#3-bir-isteğin-baştan-sona-yolculuğu)
4. [Dosya dosya: her parça ne işe yarar?](#4-dosya-dosya-her-parça-ne-işe-yarar)
5. [Katman 1 — API + Kuyruk (FastAPI + Celery + Redis)](#5-katman-1--api--kuyruk)
6. [Katman 2 — Tekilleştirme (Dedup: Bloom filter + içerik-hash)](#6-katman-2--tekilleştirme)
7. [Katman 3+4 — Nazik indirme (Fetcher)](#7-katman-34--nazik-indirme)
8. [Katman 5 — Çok kademeli dayanıklı parser](#8-katman-5--çok-kademeli-dayanıklı-parser)
9. [Depolama (SQLAlchemy + upsert)](#9-depolama)
10. [Ölçeklenme: chunk/batch dağıtım](#10-ölçeklenme-chunkbatch-dağıtım)
11. [Domain-bazlı rate limit](#11-domain-bazlı-rate-limit)
12. [Incremental (artımlı) güncelleme](#12-incremental-artımlı-güncelleme)
13. [Dayanıklılık: retry + dead-letter](#13-dayanıklılık-retry--dead-letter)
14. [İzleme: Flower](#14-i̇zleme-flower)
15. [Web Arayüzü (Kontrol Paneli)](#15-web-arayüzü-kontrol-paneli)
16. [Çalıştırma kılavuzu (3 mod)](#16-çalıştırma-kılavuzu)
17. [Test stratejisi](#17-test-stratejisi)
18. [Konfigürasyon (tüm env değişkenleri)](#18-konfigürasyon)
19. [Sık sorulan sorular](#19-sık-sorulan-sorular)
20. [Etik ve yasal](#20-etik-ve-yasal)

---

## 1. Proje ne yapıyor?

ScrapeHub, bir web sitesindeki sayfaları **otomatik olarak gezip** (crawl), her sayfadan
**yapısal veri çıkaran** (parse) ve bunu bir **veritabanına yazan** bir sistemdir. Hedef
sitemiz [books.toscrape.com](https://books.toscrape.com) — tam olarak bu amaçla, yani
scraping pratiği yapılsın diye **kasıtla yayınlanmış**, anti-bot savunması olmayan, izinli
bir demo kitap mağazasıdır.

Ama asıl amaç "kitap çekmek" değil. Asıl amaç, **gerçek dünyada ölçeklenen bir scraping
mimarisinin** nasıl kurulduğunu öğretmek:

- Binlerce sayfayı **paralel** işleme (tek tek değil, aynı anda onlarca worker).
- Aynı sayfayı **iki kez işlememe** (deduplication).
- Sunucuyu **yormama** (nezaket gecikmesi, retry/backoff, robots.txt).
- Site tasarımı **değişse bile kırılmayan** veri çıkarımı.
- Bir şey **ters giderse** (ağ hatası, çökme) sistemin kendini toparlaması.

Aynı iskelet; izin veren herhangi bir site veya açık bir API için kullanılabilir.

---

## 2. Büyük resim: dağıtık pipeline nedir?

"Pipeline" (boru hattı) = veri, bir dizi istasyondan sırayla geçer. "Dağıtık" = bu
istasyonlar tek bir programda değil, **birbirinden bağımsız çalışan parçalarda** durur ve
aralarında bir **kuyruk** üzerinden haberleşir.

Neden tek bir döngü (`for url in urls: ...`) yerine bu karmaşa? Çünkü:

- **Paralellik:** 10.000 sayfayı tek tek çekmek saatler sürer. 50 worker'a dağıtırsan
  dakikalara iner.
- **Dayanıklılık:** Bir worker çökerse iş kaybolmaz; başka worker devralır.
- **Ölçeklenme:** Yük artınca sadece worker sayısını artırırsın; kod değişmez.

Parçalar:

```
  İstemci ──HTTP──► FastAPI ──kuyruğa koy──► Redis ──dağıt──► Celery Worker'lar ──yaz──► DB
  (sen/cron)        (API)                    (broker)         (gerçek işçiler)          (Postgres/SQLite)
```

- **FastAPI (API):** Dışarıdan "şu siteyi kaz" emrini alır, işi kuyruğa koyar ve **hemen**
  döner (uzun işi beklemez — buna *asenkron* denir).
- **Redis (broker):** İşlerin biriktiği kuyruk. Aynı zamanda dedup için Bloom filter'ı tutar.
- **Celery Worker'lar:** Kuyruktaki işleri çekip asıl scraping'i yapan işçiler. Kaç tane
  olduklarını sen belirlersin.
- **Veritabanı:** Sonuçların yazıldığı yer.

---

## 3. Bir isteğin baştan sona yolculuğu

Diyelim `POST /scrape {"max_pages": 5, "chunk_size": 10}` gönderdin. Adım adım ne olur:

1. **`app/main.py`** isteği alır, `discover_catalog` görevini kuyruğa koyar (`.delay()`),
   sana bir `task_id` döndürür. (Burada henüz hiçbir sayfa çekilmedi!)
2. Bir worker kuyruktan **`discover_catalog`** görevini çeker. Katalog sayfalarını (1..5)
   gezer, her sayfadaki kitap linklerini toplar. Diyelim 100 kitap URL'i buldu.
3. Bu 100 URL'i tek tek değil, **10'arlı gruplar (chunk)** halinde tekrar kuyruğa koyar.
   Yani 10 adet `scrape_chunk` ("10 kitabı işle") görevi oluşur (broker'a 100 mesaj yerine
   10 mesaj = daha verimli).
4. Boştaki worker'lar bu chunk görevlerini kapışır. Her chunk içindeki her URL için
   **`scrape_book`** mantığı çalışır:
   - **Dedup:** Bu URL'i yakın zamanda (`DEDUP_TTL`) başarıyla işledik mi? (Bloom filter'a sor.)
     İşlediysek **atla**.
   - **Fetch:** Sayfayı nazikçe indir (retry'lı, gecikmeli). Başarısızsa URL chunk'tan çıkıp
     tek başına yeniden kuyruğa atılır → retry → dead-letter. Chunk'ın kalanı işlenmeye devam eder.
   - **Parse:** HTML'den `Book` objesi çıkar (JSON-LD → CSS → regex fallback zinciriyle).
   - **Store:** Veritabanına yaz (URL'e göre upsert: varsa güncelle, yoksa ekle). URL ancak bu
     yazım başarılı olunca "görüldü" olarak işaretlenir.
5. Sen bu sırada `GET /status/{task_id}` ile durumu, `GET /stats` ile DB'deki kitap
   sayısını sorgulayabilirsin (ikisi de varsayılanda `X-API-Key` ister). `http://localhost:5555`
   (Flower) ile worker'ları canlı izlersin.

---

## 4. Dosya dosya: her parça ne işe yarar?

```
ScrapeProjem/
├── app/
│   └── main.py            # FastAPI: dış dünyaya açılan kapı (endpoint'ler + frontend servisi)
├── frontend/
│   ├── index.html         # Web kontrol paneli
│   ├── styles.css         # Karanlık tema CSS
│   └── app.js             # Vanilla JS dashboard mantığı
├── workers/
│   ├── celery_app.py      # Celery yapılandırması (broker, retry, rate limit ayarları)
│   └── tasks.py           # Asıl görevler: discover_catalog + scrape_chunk + scrape_book (+ webhook)
├── core/
│   ├── config.py          # Tüm ayarların tek merkezi (env'den okur, default'ları var)
│   ├── dedup.py           # Tekilleştirme: Bloom filter + içerik-hash
│   ├── egress_proxy.py    # Tarayıcının tek çıkışı: SSRF denetimli HTTP/CONNECT proxy'si (compose `egress`)
│   ├── fetcher.py         # Nazik HTTP indirici (retry/backoff/robots/gecikme)
│   └── netguard.py        # Giden istek koruması (SSRF): URL/IP denetimi, yönlendirme takibi
├── parser/
│   └── extract.py         # HTML -> Pydantic Book (çok kademeli, LLM'siz)
├── storage/
│   └── db.py              # SQLAlchemy: tablo tanımı + upsert + sayım
├── tests/                 # Pytest paketi + canlı bloom doğrulama scripti
│   ├── conftest.py        # Test ortak kurulum (izole geçici DB, memory dedup)
│   ├── test_parser.py     # Parser doğru Book çıkarıyor mu? Fallback'ler çalışıyor mu?
│   ├── test_dedup.py      # İlk görüş False / ikinci True? Dilimli ömür doğru mu?
│   ├── test_db.py         # Upsert duplicate satır açmıyor mu?
│   ├── test_discover.py   # Chunk dağıtımı + webhook çalışıyor mu? (eager mod)
│   ├── test_scrape_book.py# URL yalnız başarılı yazımdan sonra mı "görüldü" sayılıyor?
│   ├── test_fetcher.py    # robots.txt (alan adı başına, önbellek) + 5xx geri çekilmesi
│   ├── test_ops_endpoints.py # /health Redis denetimi + dead-letter sırası/sınırı
│   ├── test_incremental.py# Domain gecikme + içerik-hash mantığı
│   ├── test_deadletter.py # Retry tetikleyici + dead-letter yönlendirme
│   ├── test_netguard.py   # SSRF koruması: iç adres, yönlendirme, webhook, tarayıcı
│   ├── test_egress_proxy.py # Tarayıcı çıkış proxy'si: iç hedef reddi, denetlenen IP'ye bağlanma
│   └── verify_bloom.py    # CANLI RedisBloom doğrulaması (test değil, elle çalıştırılır)
├── run_local.py           # Redis/Celery KURMADAN tüm pipeline'ı senkron çalıştırır
├── requirements.txt       # Python bağımlılıkları (çalışma zamanı)
├── requirements-dev.txt   # Test bağımlılıkları (requirements.txt + pytest vb.)
├── .env.example           # Docker için örnek ortam dosyası (sırlar burada doldurulur)
├── Dockerfile             # API/worker imajı
├── docker-compose.yml     # redis + postgres + api + worker + flower + browserless + egress
├── README.md              # Hızlı başlangıç
└── DOKUMANTASYON.md       # (bu dosya) ayrıntılı rehber
```

**Tasarım ilkesi:** Her dosya **tek bir sorumluluğa** sahip. Dedup'ı değiştirmek istersen
sadece `core/dedup.py`'a dokunursun; fetcher'dan habersizdir. Bu, "separation of concerns"
(sorumlulukların ayrılması) ilkesidir ve kodu test edilebilir/değiştirilebilir kılar.

---

## 5. Katman 1 — API + Kuyruk

**Dosyalar:** `app/main.py`, `workers/celery_app.py`, `workers/tasks.py`

### FastAPI (`app/main.py`)
Dış dünyaya açtığı endpoint'ler (`GET /` paneli ve `/static/*` dosyaları hariç):

| Endpoint | Ne yapar |
|----------|----------|
| `POST /scrape` | Katalog tarama işini kuyruğa atar (`start_url`, `max_pages`, `chunk_size`, `webhook_url`) |
| `POST /scrape-dynamic` | Aynı işi AI / tarayıcı (Playwright) moduyla kuyruğa atar |
| `POST /scrape-one` | Tek bir URL'i kuyruğa atar |
| `GET /status/{task_id}` | Bir Celery görevinin durumunu sorgular |
| `GET /stats` | DB'deki kitap sayısı + dedup backend bilgisi |
| `GET /books` | DB'deki kitaplar (sayfalı) |
| `GET /dead-letter` | Dead-letter kuyruğundaki başarısız görevler, en yeni önce (`limit`: varsayılan 100, en çok 1000) |
| `GET /health` | Redis'e kısa zaman aşımlı (2 sn) doğrudan `PING` + veritabanı denetimi; ikisi de sağlamsa `200`, değilse `503`. Yanıtta hedef site ve dedup backend da var |
| `GET /metrics` | Prometheus metrikleri |

**Kimlik doğrulama:** `POST` uçları her zaman `X-API-Key` başlığı ister. `GET /status`,
`/stats`, `/books` ve `/dead-letter` da varsayılanda anahtar ister; `PUBLIC_READ=true` bu dört
okuma ucunu anahtarsız açar. `/`, `/static/*`, `/health` ve `/metrics` açıktır. Eksik ya da
yanlış anahtar `401` döner. `API_KEY`'in varsayılanı yoktur: ayarlı değilse hiçbir istek kimlik
doğrulayamaz. Eski sürümlerde belgelerde yayımlanan sabit anahtar açıkça ayarlansa bile reddedilir.

**Girdi sınırları:** `max_pages` 1–`MAX_PAGES_LIMIT` (varsayılan 100), `chunk_size` 1–100.
`start_url`, `webhook_url` ve `url` en çok 2048 karakter olmalı, yalnız `http`/`https` şemasıyla
ve iç ağa işaret etmeyen bir ana makineyle (ör. `localhost`, `127.0.0.1`, `10.x`, `169.254.169.254`
reddedilir) gelmelidir. Uymayan istek `422` döner ve kuyruğa hiç girmez. API bu aşamada DNS
sorgusu yapmaz; asıl denetim ağa çıkılırken yapılır ([Bölüm 7](#7-katman-34--nazik-indirme)).

Kritik nokta: `/scrape` **işi yapmaz**, sadece **kuyruğa koyar** ve anında döner. Asıl iş
worker'larda arka planda olur. Bu sayede API binlerce isteği bloke olmadan karşılar.

### Celery (`workers/celery_app.py`)
Celery, "görev kuyruğu" kütüphanesidir. Yapılandırmadaki önemli ayarlar:

- `task_acks_late=True`: Görev **bittiğinde** onaylanır. Worker yarıda çökerse görev
  kuyrukta kalır, başka worker tekrar dener. (Aksi halde "alındı" der almaz çökerse iş kaybolur.)
- `task_reject_on_worker_lost=True`: Worker ölürse görev kaybolmaz, yeniden kuyruğa döner.
- `worker_prefetch_multiplier=1`: Her worker kuyruktan tek tek iş alır = adil dağıtım
  (yavaş bir worker önden 100 iş kapıp tıkamaz).
- `task_default_rate_limit`: Worker tarafında saniyede maksimum görev sayısı (nezaket).

### Görevler (`workers/tasks.py`)
Dört görev var:

- **`discover_catalog`**: Katalog sayfalarını gezip kitap URL'lerini bulur ve onları
  `scrape_chunk` görevleri (chunk'lar halinde) ya da tek tek `scrape_book` görevleri olarak
  kuyruğa koyar. (Detay: [Bölüm 10](#10-ölçeklenme-chunkbatch-dağıtım))
- **`scrape_chunk`**: Bir chunk'taki URL'leri sırayla `scrape_book` mantığıyla işler; bir URL'nin
  hatası chunk'ın kalanını düşürmez, o URL tek başına yeniden kuyruğa atılır.
- **`scrape_book`**: Tek bir kitabı işler (dedup → fetch → parse → store). Hata yönetimi
  ve incremental mod burada. (Detay: [Bölüm 13](#13-dayanıklılık-retry--dead-letter))
- **`webhook_callback`**: `webhook_url` verildiyse koşu bitince sonucu o adrese `POST` eder.

---

## 6. Katman 2 — Tekilleştirme

**Dosya:** `core/dedup.py`

İki ayrı soruya cevap verir:

### a) "Bu URL'i daha önce gördük mü?" — Bloom Filter

**Sorun:** 500.000 URL'i bir SQL tablosunda `SELECT ... WHERE url=?` ile aramak pahalıdır
(her kontrol disk/indeks erişimi).

**Çözüm:** **Bloom filter** — olasılıksal bir veri yapısı. RAM'de O(1) (nanosaniye) hızında
"bu öğeyi kesinlikle görmedim" veya "muhtemelen gördüm" cevabı verir. Tek "kusur"u: küçük,
ayarlanabilir bir yanlış-pozitif oranı (default %0.1). Yani çok nadiren "gördüm" der ama
görmemiştir; **asla** tersi olmaz ("görmedim" dediyse kesin görmemiştir). Scraping'de bu
takas mükemmeldir: nadiren bir sayfayı atlamak, milyonlarca sayfayı hızlı kontrol etmenin
yanında önemsizdir.

Çalışma modları:
- **Redis Stack varsa** → gerçek **RedisBloom** modülü (`BF.INSERT` / `BF.EXISTS` komutları).
  Devasa ölçek, RAM'de, worker'lar arasında paylaşılır.
- **Redis yoksa** → otomatik olarak **in-memory set** fallback'ine düşer (tek makinede
  ders/test için yeterli). Kod hiç değişmez; `backend` alanı `"memory"` olur.

**Ne zaman "görüldü" sayılır?** Bir URL ancak kitabı veritabanına başarıyla yazıldıktan sonra
işaretlenir (`scrape_book` önce yalnız `seen(url)` ile bakar, yazımdan sonra `mark(url)` çağırır).
Başarısız bir çekme ya da ayrıştırma URL'yi "görüldü" bırakmaz; sonraki deneme sayfayı gerçekten
yeniden çeker.

**Ne kadar süre?** Bloom filtresinden tek bir öğe silinemez. Bu yüzden zaman 8 dilime bölünür
(dilim genişliği `DEDUP_TTL / 8`, yukarı yuvarlanır) ve her dilimin kendi filtresi vardır
(`scrapehub:seen_urls:<genişlik>:<dilim no>`). URL işaretlendiği andaki dilime yazılır; her dilim
filtresi, dilimin bitişinden `DEDUP_TTL` sonra Redis'te kendiliğinden silinir. `seen()` hâlâ
yaşayan dilimlerin hepsine bakar. Sonuç: her URL en az `DEDUP_TTL`, en çok bir dilim fazlası
"görüldü" kalır (varsayılan 24 sa için 24–27 sa) ve kayıtlar dilim dilim, tek tek sona erer; tüm
durum aynı anda sıfırlanmaz. Bellek yedeği aynı dilim düzenini kullanır, yani `DEDUP_TTL` iki modu
da etkiler. `BLOOM_CAPACITY` dilimlere bölünür, `BLOOM_ERROR_RATE` de bakılan filtre sayısına
bölünerek her dilime verilir; böylece toplam bellek ve birleşik yanlış-pozitif oranı tek filtreye
yakın kalır. Dilim numarası worker'ın saatinden hesaplanır; worker saatlerinin (NTP ile) senkron
olduğu varsayılır.

> **Eski sürümden yükseltme:** Önceki sürüm tek ve ömürsüz bir `scrapehub:seen_urls` filtresi
> kullanıyordu; yeni kod ona bakmaz. Redis'te kaldıysa elle silinebilir:
> `docker compose exec redis redis-cli DEL scrapehub:seen_urls`

İlgili metotlar: `seen(url)`, `mark(url)`, `check_and_mark(url)` (görmediyse işaretle ve `False`
dön = işle; gördüyse `True` dön = atla). `check_and_mark` atomik değildir (`seen` + `mark` iki ayrı
adım) ve görevler onu kullanmaz.

> **Canlı görmek ister misin?** `tests/verify_bloom.py` bir Redis Stack'e bağlanıp
> `backend == "redis-bloom"` olduğunu, `BF.ADD/EXISTS`'in gerçekten çalıştığını ve dilimli ömrü
> (`DEDUP_TTL=4` ile, ~6 sn sürer: kayıtlar en az 4 sn görülür, sonunda Redis'ten silinir) kanıtlar.

### b) "Gördüğümüzden beri sayfa değişti mi?" — İçerik Hash

`content_changed(url, html)`: Sayfanın içeriğinin SHA-1 özetini saklar. Aynı URL tekrar
geldiğinde yeni özeti eskiyle karşılaştırır:
- Yeni URL ya da içerik değişmiş → `True` (yeniden işle) + yeni özeti sakla.
- İçerik aynı → `False` (boşuna parse/DB yazımı yapma).

`remember=False` ile yalnız karşılaştırır, özeti saklamaz; özet sonradan `remember_content(url, html)`
ile saklanır. `scrape_book` bu yolu kullanır: özet ancak kitap başarıyla yazılınca saklanır.

Bu, **incremental güncellemenin** kalbidir ([Bölüm 12](#12-incremental-artımlı-güncelleme)).

---

## 7. Katman 3+4 — Nazik indirme

**Dosya:** `core/fetcher.py`

"Stealth tarayıcı / proxy şelalesi / anti-bot bypass" YOK. İzin veren bir hedefte bunlara
gerek yoktur. Onun yerine **sağlam mühendislik**:

- **curl_cffi** ile istekler: TLS/HTTP2 parmak izi gerçek bir tarayıcınınki gibidir
  (`impersonate="chrome120"`; ikinci proxy kademesinde `safari15_3`). İstekte taklit edilen
  tarayıcının kendi User-Agent'ı gider; `USER_AGENT` ayarı bu yolu değil, yalnız tarayıcı/AI
  yolunu etkiler.
- **Retry + exponential backoff:** Ağ hatası, `403/429/503` ("yavaşla"/engel sinyali) ya da
  başka bir `5xx` yanıtında artan beklemeyle tekrar dener (en çok `MAX_RETRIES` deneme; bekleme
  `gecikme × 2^deneme`, varsayılanla 1 sn, 2 sn; son denemeden sonra beklenmez). `PROXY_TIER_1`/
  `PROXY_TIER_2` tanımlıysa ağ hatası ve `403/429/503`'te önce bir sonraki proxy kademesine geçer.
  Diğer `4xx` yanıtları yeniden denenmez. Sunucuya saygı.
- **Nezaket gecikmesi:** Her istekten sonra biraz bekler. Artık bu gecikme **domain-bazlı**
  ayarlanabilir ([Bölüm 11](#11-domain-bazlı-rate-limit)).
- **robots.txt kontrolü (`RESPECT_ROBOTS`):** Her URL için o URL'nin **kendi alan adının**
  `robots.txt`'i okunur; istek, sayfa istekleri gibi SSRF korumasından geçer. Sonuç worker süreci
  başına önbelleğe alınır: okunan (ya da kesin sonuç veren) robots.txt 1 saat, okunamayan
  (`5xx`, ağ hatası, zaman aşımı) 5 dakika. `401/403` her şeyi yasaklar, diğer `4xx` her şeyi
  serbest bırakır. robots.txt okunamazsa, o alan adı için daha önce okunan kurallar varsa dosya
  yeniden okunana kadar **son okunan kurallar** geçerli kalır (5 dakikada bir yeniden denenir);
  hiç okunamamışsa sayfa **izinli sayılır**.
- **SSRF koruması (`core/netguard.py`):** Sayfa istekleri `guarded_request` üzerinden gider. Yalnız
  `http`/`https`; ana makine adı çözülür ve çözülen adreslerden biri bile genel değilse
  (loopback, özel ağ, link-local/bulut metadata, CGNAT, IPv6 ULA vb.) istek gönderilmez.
  Doğrulanan IP curl'e sabitlenir (DNS rebinding'e karşı); curl'e özgün URL değil, ana makine
  kısmı tam olarak denetlenen adla yeniden kurulmuş URL verilir (sonda noktalı `example.com.`,
  büyük harf, IDN ya da sayısal IP yazımları sabitlemeyi atlatamaz). Yönlendirmeler elle izlenip
  her adım yeniden denetlenir (en çok 5). Politika ihlali kalıcı sayılır: yeniden denenmez, `None`
  döner. Webhook da aynı denetimden geçer ama yönlendirme izlemez. Proxy kullanılırken IP sabitleme
  uygulanmaz. `ALLOW_PRIVATE_NETWORKS=true` yerel geliştirme için IP denetimlerini kapatır (şema
  denetimi kalır).
- **Tarayıcı/AI modu (`core/browser.py`, `core/egress_proxy.py`):** Koruma ağ katmanındadır.
  Compose'da browserless yalnız `internal` olan `browser` ağındadır (worker ve `egress` ile
  birlikte): internete, host'a ve `backend` ağına (Redis/Postgres/Jaeger/Flower/API) yolu yoktur.
  Tek çıkışı `egress` servisidir: tarayıcı bağlamı (`BROWSER_EGRESS_PROXY`) tüm trafiğini ona
  yollar (HTTPS ve WebSocket için `CONNECT`, düz HTTP için mutlak URL'li istek). Proxy her
  bağlantının hedefini `check_url` ile denetler (iç/özel adres, tek etiketli ad → `403`) ve
  denetlenen IP'ye bağlanır; ana gezinme, yönlendirmeler, alt kaynaklar, fetch/XHR ve WebSocket
  tarayıcının kendi ağ yığınıyla, ama tek tek denetlenerek gider (DNS rebinding yok: adı
  proxy bir kez çözer). Hedef URL tarayıcıya verilmeden önce worker'da da denetlenir, gezinmenin
  yönlendirme zinciri sonradan kontrol edilir; service worker'lar kapalıdır. Tüm oturum
  1,5 × `REQUEST_TIMEOUT` sonunda kesilir (ana iş parçacığını kilitleyen sayfaya karşı).
  `BROWSER_EGRESS_PROXY` boşsa (compose dışı) yalnız uygulama içi URL süzgeci çalışır. Kalan
  sınırlar README'deki "Known limitations" listesindedir.

`get(url)` başarısızsa `None` döndürür; çağıran taraf (görev) buna göre retry/dead-letter
kararı verir.

---

## 8. Katman 5 — Çok kademeli dayanıklı parser

**Dosya:** `parser/extract.py`

**Felsefe:** "Site tasarımı değişse de parser çalışmaya devam etsin, hiçbir API anahtarı
gerekmesin." Eskiden bir LLM parser'ı vardı; **kaldırıldı**. Yerine her alan için **üç
kademeli fallback** zinciri kuruldu. Bir kaynak başarısız olursa diğerine düşer:

1. **Yapısal veri:** `<script type="application/ld+json">` (schema.org) ve `og:`/`<meta>`
   etiketleri. Sayfanın görünümünden bağımsız, en sağlam kaynak.
2. **CSS seçiciler:** Aynı alan için **birden fazla** yedek seçici. Biri kırılırsa diğeri yakalar.
3. **Regex/pattern:** Son çare. Ham metinden desenle çek (ör. `£51.77`, `22 available`,
   `4 out of 5`).

Her kademe `Optional` döndürür; **ilk None-olmayan değer kazanır** (`_first(...)` yardımcısı).
Pydantic `Book` modeli **değişmedi** (title, price, availability, rating, url, description).

Örnek — `title` alanının fallback zinciri:
```
JSON-LD "name"  →  og:title/meta  →  div.product_main h1 / h1 / article h1  →  <title> etiketi
```

Bu sayede tek bir CSS seçici değişse bile alan boş kalmaz. `parse_html()` title'ı hiçbir
kaynaktan bulamazsa (sayfa tanınmıyor) `None` döner ve görev `parse_failed` olur.

---

## 9. Depolama

**Dosya:** `storage/db.py`

- **SQLAlchemy** ile veritabanı soyutlanır. `DATABASE_URL` Postgres'e işaret ediyorsa
  Postgres, etmiyorsa **SQLite** kullanılır. Böylece repoyu klonlayan kişi **sıfır kurulumla**
  (SQLite ile) çalıştırır; prodüksiyonda tek satır env değiştirip Postgres'e geçer.
- `BookRow` tablosunda `url` üzerinde tek bir **unique index** (`ix_books_url`) var → aynı URL
  iki kez yazılamaz **ve** upsert'teki `WHERE url = ?` araması indeksli/hızlı olur.
- **`upsert_book(book)`:** URL'e göre "upsert" — satır varsa **günceller**, yoksa **ekler**.
  Bu sayede aynı kitabı tekrar işlersen DB'de **duplicate satır oluşmaz**, sadece güncellenir.

---

## 10. Ölçeklenme: chunk/batch dağıtım

**Dosya:** `workers/tasks.py` → `discover_catalog`

**Sorun:** 10.000 kitap URL'ini tek tek `.delay()` ile kuyruğa atmak = broker'a 10.000 ayrı
mesaj = gereksiz yük.

**Çözüm:** URL'leri `chunk_size`'lık dilimlere bölüp her dilim için bir `scrape_chunk`
görevi kurar, hepsini tek bir Celery `group` olarak kuyruğa atarız:
```python
group(
    scrape_chunk.s(book_urls[i:i + chunk_size], use_ai)
    for i in range(0, len(book_urls), chunk_size)
).apply_async()
```
`chunk_size=10` ise 1000 URL → 100 görev (her görev 10 URL işler). Çok daha az mesaj, daha
iyi throughput.

`scrape_chunk`, Celery'nin `.chunks()` özelliğinin yerini alır; farkı şudur: bir URL hata
verirse chunk'ın kalanı düşmez. Hata alan URL tek başına bir `scrape_book` görevi olarak yeniden
kuyruğa atılır (yeniden deneme ve dead-letter oradan gelir) ve chunk sonucuna `"requeued"` olarak
yazılır.

**Geriye uyumluluk:** `chunk_size <= 1` verilirse eski davranışa (her URL için ayrı
`scrape_book.delay()`) düşer. Böylece küçük demo akışı hiç değişmeden çalışır. Parametre
`POST /scrape`'ten ayarlanabilir.

**Webhook:** `webhook_url` verildiyse koşu başına **tam bir kez** gönderilir: chunk modunda tüm
chunk'lar bitince (Celery `chord`), per-URL modunda tek URL'lik `scrape_chunk` görevleri
bitince, katalogdan hiç URL çıkmadıysa da hemen. Gövde:
```json
{"event": "scraping_completed",
 "meta": {"pages_scanned": 5, "books_dispatched": 100, "ai_used": false},
 "total_processed": 100, "successful_inserts": 97, "requeued": 2}
```
`total_processed` işlenen URL sayısıdır; `successful_inserts` sonucu `ok`/`updated` olanlar,
`requeued` tek başına yeniden kuyruğa atılanlardır (onların sonucu webhook'tan sonra gelir).

---

## 11. Domain-bazlı rate limit

**Dosyalar:** `core/config.py` (`request_delay_for`), `core/fetcher.py`

Farklı siteler farklı hassasiyettedir. Tek bir global gecikme yerine **domain başına** ayrı
gecikme tanımlanabilir. `DOMAIN_DELAYS` env'i:
```
DOMAIN_DELAYS="books.toscrape.com:0.5,example.com:2.0"
```
`settings.request_delay_for(url)`, URL'in domain'ine bakar; tanımlıysa ona özel gecikmeyi,
değilse genel `REQUEST_DELAY` default'unu döndürür (`www.` öneki toleranslı). Fetcher hem
nezaket gecikmesinde hem backoff hesabında bu değeri kullanır.

---

## 12. Incremental (artımlı) güncelleme

**Dosyalar:** `core/dedup.py` (`content_changed`), `workers/tasks.py` (`scrape_book`)

**Senaryo:** Aynı katalogu her gün çekiyorsun. Çoğu sayfa **değişmedi**; sadece bazı
fiyat/stok güncellendi. Hepsini baştan parse edip DB'ye yazmak israf.

**Çözüm:** `INCREMENTAL=true` olduğunda `scrape_book` şöyle davranır:
- URL daha önce görülmüş olsa **bile** sayfayı çeker.
- `content_changed(url, html)` ile içeriği önceki özetle karşılaştırır.
  - Değişmemiş → `skipped_unchanged` (parse/DB yazımı yok, kaynak tasarrufu).
  - Değişmiş → yeniden parse + `upsert` → `updated`.
- Yeni içerik özeti ancak kitap DB'ye başarıyla yazıldıktan sonra saklanır. Ayrıştırma ya da DB
  yazımı başarısız olursa özet eskisi gibi kalır ve sayfa bir sonraki koşuda yeniden işlenir
  ("değişmemiş" sanılıp atlanmaz).

`INCREMENTAL` kapalıyken (default) klasik URL-bazlı bloom davranışı aynen geçerlidir.
İki mekanizmanın farkı: **Bloom** = "bu URL'i hiç gördük mü?"; **content-hash** = "gördüğümüzden
beri değişti mi?".

---

## 13. Dayanıklılık: retry + dead-letter

**Dosyalar:** `workers/tasks.py`, `workers/celery_app.py`

Gerçek dünyada istekler başarısız olur (ağ kesintisi, geçici 503...). Sistem bununla baş
etmeli:

- **Otomatik retry:** `scrape_book` fetch başarısız olunca `FetchError` fırlatır. Celery
  bunu yakalayıp **üstel backoff + jitter** (gürültü) ile `max_retries` kez yeniden dener.
  ```python
  @shared_task(autoretry_for=(FetchError,), retry_backoff=True, retry_jitter=True, ...)
  ```
  (jitter, tüm worker'ların aynı anda tekrar denemesini — "thundering herd" — önler.)
  Chunk modunda URL'ler `scrape_chunk` içinde doğrudan işlendiği için orada autoretry çalışmaz;
  hata alan URL tek başına bir `scrape_book` görevi olarak yeniden kuyruğa atılır ve retry o
  görevde olur ([Bölüm 10](#10-ölçeklenme-chunkbatch-dağıtım)).
- **Dead-letter:** Tüm retry'lar tükenip görev **kalıcı** başarısız olunca, özel
  `DeadLetterTask.on_failure` devreye girer ve başarısız görevi bir **dead-letter kuyruğuna**
  (`scrapehub:dead_letter` redis listesi) yazar: `task`, `task_id`, `args`, `kwargs` (ör.
  `/scrape-one`'ın URL'si) ve `error`. Listede en çok son 1000 kayıt tutulur; daha eskiler atılır.
  Redis yoksa loga düşer. `GET /dead-letter` kayıtları en yeniden eskiye döndürür; operatör
  buradan başarısızları inceleyip elle yeniden işleyebilir.
- **Worker çökmesi:** `task_acks_late` + `task_reject_on_worker_lost` sayesinde bir worker
  iş ortasında ölürse görev kaybolmaz, başka worker'a geçer.

---

## 14. İzleme: Flower

**Dosya:** `docker-compose.yml` (flower servisi)

**Flower**, Celery için web tabanlı bir izleme arayüzüdür. `docker compose up` ile gelir;
`http://localhost:5555` adresinden:
- Aktif/biten/başarısız görevleri,
- Worker'ların canlı durumunu, yükünü,
- Görev başına süreleri ve sonuçları
canlı izlersin. Docker'da Flower kullanıcı adı/parola ister (`FLOWER_USER`, varsayılan `admin`;
`FLOWER_PASSWORD`, `.env`'den) ve yalnız `127.0.0.1`'den erişilebilir. Docker'sız çalıştırmak için:
```bash
celery -A workers.celery_app.celery_app flower --port=5555
```

---

## 15. Web Arayüzü (Kontrol Paneli)

**Dosyalar:** `frontend/index.html`, `frontend/styles.css`, `frontend/app.js`, `app/main.py` (static mount + CORS)

API'ye entegre, modern ve karanlık tema bir kontrol paneli eklendi. `docker compose up` veya `uvicorn app.main:app` çalıştırdıktan sonra tarayıcıdan `http://localhost:8000` adresine giderek kullanabilirsiniz.

Arayüz üzerinden yapabilecekleriniz:

- **Dashboard:** DB'deki kitap sayısı, işlenen/başarısız sayfa, dead-letter derinliği ve ham Prometheus metrikleri.
- **Kazıma Başlat:**
  - Katalog kazıma (start URL, max sayfa, chunk boyutu, webhook).
  - Tek URL kazıma (force seçeneği ile Bloom Filter'ı atla).
  - AI destekli dinamik kazıma (Playwright + Llama).
- **Görevler:** Celery task ID ile durum sorgulama ve son görev geçmişi.
- **Kitaplar:** Veritabanındaki kitapları tablo halinde görüntüleme ve sayfalama.
- **Dead Letter:** Başarısız görevleri ve hata mesajlarını inceleme.
- **Sağlık:** Redis ve PostgreSQL bağlantı durumu.

Teknik detaylar:

- FastAPI `StaticFiles` ile `frontend/` dizini `/static` altında servis edilir.
- `GET /` isteği `frontend/index.html` dosyasını döndürür.
- CORS varsayılanda kapalıdır; panel API ile aynı origin'den servis edildiği için gerekmez.
  Frontend'i ayrı bir domainden çalıştıracaksan o origin'i `CORS_ORIGINS`'e ekle.
- API Key, tarayıcının `localStorage`'ında saklanır; sayfa yenilense bile korunur.
- Anahtar girilmemişse ya da sunucu reddederse (`401`) panel bir uyarı bandı gösterir.
- Sunucudan gelen veri (kitap adları, hata mesajları vb.) HTML olarak yorumlanmaz, düz metin
  olarak basılır; bağlantılar yalnız `http`/`https` ise oluşturulur. `index.html` ayrıca
  satır içi betiği engelleyen bir Content-Security-Policy taşır.

---

## 16. Çalıştırma kılavuzu

### Mod 1 — Sıfır altyapı (en kolay)
Redis/Celery KURMADAN tüm pipeline'ı **senkron** çalıştırır (öğrenmek için ideal):
```bash
pip install -r requirements.txt
python run_local.py --pages 2
```
Çıktıda dedup backend'ini, çekilen kitapları ve toplam sayıyı görürsün; SQLite'a yazar.

### Mod 2 — Tam dağıtık (Docker)
Önce `.env` dosyasını oluşturup zorunlu sırları doldur: `API_KEY`, `REDIS_PASSWORD`,
`POSTGRES_PASSWORD`, `BROWSERLESS_TOKEN`, `FLOWER_PASSWORD`. Parolalar bağlantı URL'lerine
yazıldığı için yalnız harf/rakam kullan (`secrets.token_hex` çıktısı uygundur). Biri eksikse
`docker compose` hangisinin eksik olduğunu söyleyip durur.
```bash
cp .env.example .env      # Windows: copy .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # her sır için bir değer
docker compose up --build
```
Sonra (`$API_KEY` = `.env`'e yazdığın değer):
```bash
curl -X POST localhost:8000/scrape -H "Content-Type: application/json" \
     -H "X-API-Key: $API_KEY" -d '{"max_pages": 5, "chunk_size": 10}'
curl -H "X-API-Key: $API_KEY" localhost:8000/stats
# Flower: http://localhost:5555 (FLOWER_USER / FLOWER_PASSWORD)
```
Tüm portlar yalnız `127.0.0.1`'e bağlıdır. API'yi ağdaki başka makinelere açmak için `.env`
içinde `API_BIND_ADDR=0.0.0.0` yap.

### Mod 3 — Manuel dağıtık (Docker olmadan, ayrı terminaller)
```bash
docker run -p 6379:6379 redis/redis-stack:7.4.0-v8                                 # 1) Redis Stack
celery -A workers.celery_app.celery_app worker --loglevel=info --concurrency=4     # 2) Worker
uvicorn app.main:app --reload                                                      # 3) API
celery -A workers.celery_app.celery_app flower --port=5555                         # 4) (ops.) Flower
```
API'yi başlatmadan önce `API_KEY` ortam değişkenini ayarla; yoksa korumalı uçlar her isteği
`401` ile reddeder.

AI/tarayıcı modu için browserless'ı compose dışında kendin çalıştırıyorsan worker'a
`BROWSERLESS_URL` ile adresini ver. `BROWSER_EGRESS_PROXY` boşsa tarayıcı yolunda yalnız
uygulama içi URL süzgeci vardır (yönlendirme adımlarını, açılır pencereleri, tarayıcının DNS
çözümlemesini, WebSocket ve WebRTC'yi denetleyemez). Tam koruma için compose'daki düzeni kur: browserless'ın
tek çıkışı `python -m core.egress_proxy` olsun, `BROWSER_EGRESS_PROXY`'ye proxy'nin
browserless'tan görünen adresini yaz ve browserless'a
`DEFAULT_LAUNCH_ARGS=["--webrtc-ip-handling-policy=disable_non_proxied_udp"]` ver (WebRTC UDP'si
proxy'den geçmez).

---

## 17. Test stratejisi

```bash
pip install -r requirements-dev.txt
pytest -q
```

`requirements-dev.txt`, `requirements.txt`'in üstüne test araçlarını ekler; uygulamayı
çalıştırmak için `requirements.txt` yeterlidir.

`tests/conftest.py` testleri **izole** eder: `DATABASE_URL`'deki PostgreSQL'i kullanır (varsayılan
`postgresql+psycopg2://scrape:scrape@localhost:5432/scrapehub`), dedup'ı **memory** backend'ine
zorlar (gerçek Redis gerektirmez) ve güvenlik ayarlarını varsayılana çekip teste özgü bir
`API_KEY` verir. Her test öncesi tablolar sıfırlanır.

| Test dosyası | Neyi kanıtlar |
|--------------|---------------|
| `test_parser.py` | HTML'den doğru `Book` çıkarımı + JSON-LD/meta/regex fallback'leri |
| `test_dedup.py` | İlk görüş `False`, ikinci görüş `True`; URL normalizasyonu; dilimli ömür (en az `DEDUP_TTL`, kayıtlar tek tek düşer, sonunda silinir) |
| `test_db.py` | Aynı URL iki kez upsert → tek satır (duplicate yok), alanlar güncellenir |
| `test_discover.py` | Chunk dağıtımı (eager mod): 5 kitap → 3 chunk → hepsi DB'de; per-url fallback; bir URL'nin hatası chunk'ı düşürmez; webhook her modda tam bir kez ve URL sayısıyla; ayrıştırılamayan katalog bağlantısı atlanır, koşuyu düşürmez |
| `test_scrape_book.py` | URL ve içerik özeti yalnız başarılı yazımdan sonra işaretlenir; başarısız deneme yeniden çekilir; `force`; dead-letter `kwargs` |
| `test_fetcher.py` | robots.txt alan adı başına ve önbellekli; okunamazsa son okunan kurallar, hiç okunamamışsa izinli; `5xx`'te geri çekilip yeniden deneme; son denemeden sonra bekleme yok |
| `test_ops_endpoints.py` | `/health` Redis denetimi (Redis kapalıyken hızlı `false`); dead-letter en yeni önce ve sınırlı |
| `test_incremental.py` | Domain-bazlı gecikme + `content_changed` (yeni→aynı→değişti) |
| `test_deadletter.py` | Fetch hatası `FetchError` fırlatır; `on_failure` dead-letter'a yazar |
| `test_main.py` | API endpoint'leri (frontend, stats, books, health, metrics, yetkilendirme, girdi sınırları, iç ağ URL'lerinin reddi, CORS) |
| `test_netguard.py` | SSRF koruması: IP sınıflandırma, iç adrese yönlendirme, webhook, tarayıcı yolu (proxy ayarı, yedek URL süzgeci, oturum süre sınırı) |
| `test_egress_proxy.py` | Tarayıcı çıkış proxy'si: düz HTTP ve `CONNECT` ile iç/özel hedef, tek etiketli ad ve sayısal IP yazımları reddedilir; bağlantı denetlenen IP'ye açılır (rebinding); `ALLOW_PRIVATE_NETWORKS` |

Ayrıca `tests/verify_bloom.py` (test değil, elle çalıştırılır): **canlı** bir Redis Stack'e
karşı `backend == "redis-bloom"`, `BF.ADD/EXISTS` davranışını ve dilimli dedup ömrünü kanıtlar:
```bash
REDIS_URL=redis://localhost:6379/0 python tests/verify_bloom.py
```

---

## 18. Konfigürasyon

Tüm ayarlar `core/config.py`'da toplanır; her biri bir env değişkeninden okunur ve makul bir
default'a sahiptir. Tek istisna `API_KEY`: varsayılanı yoktur; `run_local.py` ve worker onsuz
çalışır ama API'nin korumalı uçları onsuz her isteği `401` ile reddeder.

| Değişken | Default | Açıklama |
|----------|---------|----------|
| `SCRAPE_BASE_URL` | `https://books.toscrape.com/` | Hedef site |
| `REDIS_URL` | `redis://localhost:6379/0` | Kuyruk + Bloom filter |
| `DATABASE_URL` | `sqlite:///scrapehub.db` | Postgres'e çevrilebilir |
| `REQUEST_DELAY` | `0.5` | İstekler arası saniye (genel default nezaket gecikmesi) |
| `DOMAIN_DELAYS` | (boş) | Domain-bazlı gecikme: `domain:sn,domain:sn` |
| `MAX_CONCURRENCY` | `4` | Eşzamanlı istek / worker rate limit tabanı |
| `REQUEST_TIMEOUT` | `20` | İstek zaman aşımı (sn) |
| `MAX_RETRIES` | `3` | Maksimum yeniden deneme |
| `RESPECT_ROBOTS` | `true` | robots.txt'e uy |
| `USER_AGENT` | `Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0 Safari/537.36` | Yalnız tarayıcı/AI yolunun User-Agent'ı (fetcher taklit edilen tarayıcının kendi UA'sını kullanır) |
| `PROXY_TIER_1` | (boş) | Fetcher'ın ağ hatası / `403/429/503`'te geçtiği 1. proxy kademesi |
| `PROXY_TIER_2` | (boş) | 2. proxy kademesi (`safari15_3` taklidiyle) |
| `DEDUP_TTL` | `86400` | Bir URL'nin "görüldü" kalma süresi (sn): en az bu kadar, en çok bir dilim (`DEDUP_TTL/8`) fazlası; Redis ve bellek modunda |
| `BLOOM_CAPACITY` | `1000000` | Bloom filter toplam kapasitesi (8 dilime bölünür) |
| `BLOOM_ERROR_RATE` | `0.001` | Bloom yanlış-pozitif oranı (birleşik hedef) |
| `INCREMENTAL` | `false` | `true` → içerik değiştiyse yeniden işle |
| `CONTENT_HASH_TTL` | `604800` | Incremental içerik hash saklama süresi (sn) |
| `API_KEY` | (yok, zorunlu) | `X-API-Key` ile karşılaştırılan anahtar; boşsa korumalı uçlar `401` |
| `PUBLIC_READ` | `false` | `true` → `GET /status`, `/stats`, `/books`, `/dead-letter` anahtarsız |
| `CORS_ORIGINS` | (boş) | Virgülle ayrılmış izinli origin'ler; boş = çapraz origin kapalı |
| `ALLOW_PRIVATE_NETWORKS` | `false` | `true` → iç/özel ağ hedeflerine izin (yalnız yerel geliştirme) |
| `MAX_PAGES_LIMIT` | `100` | API'nin kabul ettiği en büyük `max_pages` |
| `BROWSERLESS_URL` | `ws://localhost:3000` | Tarayıcı/AI modu için browserless (headless Chrome) adresi |
| `BROWSER_EGRESS_PROXY` | (boş) | Tarayıcının tüm trafiğini yolladığı SSRF denetimli proxy (`core/egress_proxy.py`), browserless'tan görünen adresle; compose'da `http://egress:8899`. Boş = yalnız URL süzgeci |
| `OLLAMA_URL` | `http://127.0.0.1:11434` | AI ayrıştırma için yerel Ollama (LLM) adresi |
| `JAEGER_ENDPOINT` | `http://localhost:4318/v1/traces` | OpenTelemetry trace'lerinin gönderildiği OTLP HTTP adresi |

Docker Compose ayrıca `.env`'den `REDIS_PASSWORD`, `POSTGRES_PASSWORD`, `BROWSERLESS_TOKEN`,
`FLOWER_PASSWORD` (zorunlu), `POSTGRES_USER`, `POSTGRES_DB`, `FLOWER_USER` ve `API_BIND_ADDR`
(varsayılan `127.0.0.1`) okur; ayrıntı `.env.example`'da. `PUBLIC_READ`, `CORS_ORIGINS`,
`ALLOW_PRIVATE_NETWORKS`, `MAX_PAGES_LIMIT` ve `OLLAMA_URL` (compose'da varsayılan
`http://host.docker.internal:11434`) `.env`'den api/worker/flower'a geçirilir
(`ALLOW_PRIVATE_NETWORKS` ayrıca `egress`'e); `REDIS_URL`, `DATABASE_URL`, `BROWSERLESS_URL`,
`BROWSER_EGRESS_PROXY` ve `JAEGER_ENDPOINT` compose'da servis adlarıyla kurulur.

`POST /scrape` gövdesi: `start_url`, `max_pages` (1–`MAX_PAGES_LIMIT`), `chunk_size` (1–100),
`webhook_url`.

---

## 19. Sık sorulan sorular

**S: Neden LLM parser kaldırıldı?**
C: API anahtarı/maliyet gerektiriyordu ve deterministik değildi. Yerine gelen çok kademeli
(JSON-LD → CSS → regex) parser, çoğu gerçek sitede LLM kadar dayanıklıdır ve **bedavadır**.

**S: Bloom filter yanlış-pozitif verirse veri kaybeder miyim?**
C: Çok nadiren bir sayfayı "gördüm" sanıp atlayabilir (oran ayarlanabilir, default %0.1).
Kritik veride bu kabul edilebilir bir takastır; istersen `BLOOM_ERROR_RATE`'i düşürürsün.

**S: Redis olmadan çalışır mı?**
C: Evet. Dedup memory'e, DB SQLite'a düşer. `python run_local.py` ile sıfır altyapıda çalışır.
Dağıtık mod (Celery) için Redis gerekir.

**S: Başka bir siteyi kazıyabilir miyim?**
C: Teknik olarak `SCRAPE_BASE_URL`'i değiştirmen yeter. Ama **önce** o sitenin kullanım
şartlarını, robots.txt'ini ve yasal durumu kontrol et ([Bölüm 20](#20-etik-ve-yasal)).

**S: `chunk_size` kaç olmalı?**
C: Küçük demolarda 1 (per-url, izlemesi kolay). Büyük ölçekte 10–50 arası iyi bir başlangıç;
çok büyük olursa bir chunk uzun sürer ve paralellik düşer.

---

## 20. Etik ve yasal

- Hedef site (books.toscrape.com) scraping pratiği için **kasıtla** yayınlanmıştır.
- `core/fetcher.py` **robots.txt'e saygı** duyar ve **nezaket gecikmesi** uygular. robots.txt
  okunamazsa son okunan kurallar geçerli kalır, hiç okunamamışsa sayfa izinli sayılır
  ([Bölüm 7](#7-katman-34--nazik-indirme)).
- Başka bir siteye yönlendirirken: kullanım şartlarını, robots.txt'i ve ilgili veri koruma
  mevzuatını (ör. **KVKK/GDPR**) kontrol et. **Anti-bot savunması olan sitelerde izinsiz
  kullanma.** Bu proje, izin veren hedeflerde doğru mühendisliği öğretmek içindir; izinsiz
  scraping veya savunma atlatma için değildir.
