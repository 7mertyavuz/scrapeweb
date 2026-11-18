FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Playwright'ın paketlediği Node sürücüsü libatomic1 ister (slim imajda yok);
# o olmadan browserless'a bağlanma (fetch_with_browser) her zaman başarısız olur.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# Bağımlılıklar ayrı katmanda: kod değişince yeniden kurulmasın.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Root olmayan kullanıcı. Kod root'a ait kalır (salt okunur), uygulama
# yalnız okur; böylece süreç ele geçirilse bile kendi kodunu değiştiremez.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app

# Neyin kopyalanacağını .dockerignore belirler (.git, .env, *.db, testler girmez).
COPY . .

USER app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
