// ScrapeHub Frontend — Vanilla JS Dashboard
//
// Güvenlik notu: sunucudan (ve dolayısıyla kazınan sayfalardan) gelen hiçbir
// değer HTML olarak yorumlanmaz. Tüm dinamik içerik DOM API'leriyle
// (createElement / textContent / setAttribute) basılır; bağlantılar yalnızca
// http/https şemalı URL'ler için oluşturulur. Yeni kod eklerken sunucu verisini
// innerHTML / insertAdjacentHTML / outerHTML ile basmayın.
(function () {
    'use strict';

    const API_BASE = window.location.origin;
    const LS_KEY = 'scrapehub_api_key';

    let currentBooksOffset = 0;
    const BOOKS_LIMIT = 25;

    const PAGE_TITLES = {
        dashboard: 'Dashboard',
        scrape: 'Kazıma Başlat',
        tasks: 'Görevler',
        books: 'Kitaplar',
        deadletter: 'Dead Letter',
        health: 'Sağlık',
    };

    // DOM helpers
    const $ = (sel) => document.querySelector(sel);
    const $$ = (sel) => document.querySelectorAll(sel);

    /**
     * Güvenli eleman oluşturucu. `text` her zaman textContent olarak atanır;
     * `attrs` yalnızca sabit (kod içinde yazılmış) nitelik adları için kullanılmalı.
     */
    function el(tag, options = {}, children = []) {
        const node = document.createElement(tag);
        if (options.className) node.className = options.className;
        if (options.text !== undefined) node.textContent = toText(options.text);
        if (options.attrs) {
            Object.entries(options.attrs).forEach(([name, value]) => node.setAttribute(name, String(value)));
        }
        children.forEach((child) => node.append(child));
        return node;
    }

    /** Sunucudan gelen herhangi bir değeri düz metne çevirir (türüne güvenmeden). */
    function toText(value) {
        if (value === null || value === undefined) return '';
        if (typeof value === 'string') return value;
        if (typeof value === 'number' || typeof value === 'boolean') return String(value);
        try {
            return JSON.stringify(value);
        } catch (e) {
            return String(value);
        }
    }

    /** Yalnızca mutlak http/https URL'leri kabul eder; aksi halde null döner. */
    function safeHttpUrl(value) {
        if (typeof value !== 'string' || value.trim() === '') return null;
        let parsed;
        try {
            parsed = new URL(value.trim());
        } catch (e) {
            return null;
        }
        if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
        return parsed.href;
    }

    // API key (localStorage erişimi gizli pencerede ya da engelli olabilir)
    function readStoredKey() {
        try {
            return localStorage.getItem(LS_KEY) || '';
        } catch (e) {
            return '';
        }
    }

    function getApiKey() {
        return readStoredKey() || $('#api-key').value.trim();
    }

    function setApiKey(key) {
        try {
            if (key) localStorage.setItem(LS_KEY, key);
            else localStorage.removeItem(LS_KEY);
        } catch (e) {
            // Saklanamadıysa alan değeri yine de kullanılır.
        }
        $('#api-key').value = key;
    }

    function showToast(title, message, type = 'info') {
        const allowed = ['info', 'success', 'error'];
        const toast = el('div', { className: `toast ${allowed.includes(type) ? type : 'info'}` }, [
            el('div', { className: 'toast-title', text: title }),
            el('div', { className: 'toast-message', text: message }),
        ]);
        $('#toast-container').appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    // API
    class ApiError extends Error {
        constructor(message, status, data) {
            super(message);
            this.name = 'ApiError';
            this.status = status;
            this.data = data;
        }
    }

    function isAuthError(err) {
        return err instanceof ApiError && (err.status === 401 || err.status === 403);
    }

    /** FastAPI `detail` alanı metin, liste (422) ya da nesne olabilir. */
    function formatDetail(detail) {
        if (typeof detail === 'string') return detail;
        if (Array.isArray(detail)) {
            return detail
                .map((d) => (d && typeof d === 'object' && 'msg' in d ? toText(d.msg) : toText(d)))
                .join('; ');
        }
        return detail === undefined || detail === null ? '' : toText(detail);
    }

    async function apiCall(path, options = {}) {
        const key = getApiKey();
        const headers = {
            'Content-Type': 'application/json',
            ...options.headers,
        };
        if (key) headers['X-API-Key'] = key;

        const res = await fetch(`${API_BASE}${path}`, { ...options, headers });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            const detail = data && typeof data === 'object' ? formatDetail(data.detail) : '';
            const err = new ApiError(detail || `${res.status} ${res.statusText}`, res.status, data);
            if (isAuthError(err)) showAuthRequired(Boolean(key));
            throw err;
        }
        return data;
    }

    async function apiGet(path) {
        return apiCall(path, { method: 'GET' });
    }

    async function apiPost(path, body) {
        return apiCall(path, { method: 'POST', body: JSON.stringify(body) });
    }

    /** Kullanıcıya gösterilecek hata metni; yetki hatalarında anlaşılır yönlendirme verir. */
    function errorText(err) {
        if (isAuthError(err)) {
            return getApiKey()
                ? 'API anahtarı geçersiz. Sol menüdeki API Key alanından doğru anahtarı girip Kaydet\'e basın.'
                : 'Bu işlem için API anahtarı gerekli. Sol menüdeki API Key alanına anahtarı girip Kaydet\'e basın.';
        }
        return `Hata: ${err && err.message ? err.message : 'Bilinmeyen hata'}`;
    }

    // Yetki uyarısı
    function showAuthRequired(hadKey) {
        const banner = $('#auth-banner');
        $('#auth-banner-title').textContent = hadKey ? 'API anahtarı geçersiz' : 'API anahtarı gerekli';
        $('#auth-banner-message').textContent = hadKey
            ? 'Sunucu kayıtlı API anahtarını reddetti. Sol menüdeki API Key alanından doğru anahtarı girip Kaydet\'e basın.'
            : 'Bu panelin verileri API anahtarı ile korunuyor. Sol menüdeki API Key alanına anahtarı girip Kaydet\'e basın.';
        banner.classList.remove('hidden');
        $('.api-key-box').classList.add('needs-key');
    }

    function hideAuthRequired() {
        $('#auth-banner').classList.add('hidden');
        $('.api-key-box').classList.remove('needs-key');
    }

    // Navigation
    function initNavigation() {
        $$('.nav-item').forEach((item) => {
            item.addEventListener('click', (e) => {
                e.preventDefault();
                const page = item.dataset.page;
                history.pushState(null, '', `#${page}`);
                showPage(page);
            });
        });

        window.addEventListener('popstate', () => routeByHash());
        routeByHash();
    }

    function routeByHash() {
        showPage(window.location.hash.replace('#', ''));
    }

    function currentPageId() {
        const active = $('.page.active');
        return active ? active.id.replace('page-', '') : 'dashboard';
    }

    function showPage(requested) {
        // Hash kullanıcı/saldırgan kontrolünde: yalnızca bilinen sayfa adları seçiciye girer.
        const pageId = Object.prototype.hasOwnProperty.call(PAGE_TITLES, requested) ? requested : 'dashboard';

        $$('.page').forEach((p) => p.classList.remove('active'));
        $$('.nav-item').forEach((n) => n.classList.remove('active'));

        const page = $(`#page-${pageId}`);
        if (page) page.classList.add('active');

        const nav = $(`.nav-item[data-page="${pageId}"]`);
        if (nav) nav.classList.add('active');

        $('#page-title').textContent = PAGE_TITLES[pageId];

        if (pageId === 'dashboard') loadDashboard();
        if (pageId === 'books') loadBooks(currentBooksOffset);
        if (pageId === 'deadletter') loadDeadLetter();
        if (pageId === 'health') loadHealth();
    }

    // Dashboard
    async function loadDashboard() {
        try {
            const stats = await apiGet('/stats');
            $('#stat-books').textContent = toText(stats.books_in_db ?? '-');
        } catch (e) {
            $('#stat-books').textContent = '-';
        }

        try {
            const metricsText = await fetch(`${API_BASE}/metrics`).then((r) => r.text());
            $('#metrics-output').textContent = metricsText;

            const processed = metricsText.match(/scrapehub_pages_processed_total\s+(\d+)/);
            const failed = metricsText.match(/scrapehub_failed_fetch_total\s+(\d+)/);
            const deadletter = metricsText.match(/scrapehub_dead_letter_depth\s+(\d+)/);

            $('#stat-processed').textContent = processed ? processed[1] : '-';
            $('#stat-failed').textContent = failed ? failed[1] : '-';
            $('#stat-deadletter').textContent = deadletter ? deadletter[1] : '-';
        } catch (e) {
            $('#metrics-output').textContent = 'Metrikler alınamadı.';
        }

        renderHealth();
    }

    function healthItem(cls, label) {
        return el('div', { className: `health-item ${cls}` }, [
            el('span', { className: 'status-dot' }),
            el('span', { text: label }),
        ]);
    }

    /** /health sağlıksızken 503 ile ama aynı gövdeyle döner; gövdeyi her iki durumda da kullan. */
    async function fetchHealth() {
        try {
            return await apiGet('/health');
        } catch (err) {
            if (err instanceof ApiError && err.status === 503 && err.data && typeof err.data === 'object') {
                return err.data;
            }
            throw err;
        }
    }

    async function renderHealth() {
        const grid = $('#health-grid');
        try {
            const health = await fetchHealth();
            const checks = health && typeof health.checks === 'object' && health.checks !== null ? health.checks : {};
            const items = Object.entries(checks).map(([name, ok]) =>
                ok === true ? healthItem('ok', `${name} — Sağlıklı`) : healthItem('fail', `${name} — Hata`),
            );
            grid.replaceChildren(...items);
        } catch (e) {
            grid.replaceChildren(healthItem('fail', 'Sağlık kontrolü başarısız'));
        }
    }

    // Scrape forms
    function showResult(resultBox, ok, text) {
        resultBox.classList.remove('hidden');
        resultBox.classList.toggle('error', !ok);
        resultBox.textContent = text;
    }

    function initScrapeForms() {
        $('#scrape-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const resultBox = $('#scrape-result');
            const body = {
                start_url: $('#scrape-start-url').value || null,
                max_pages: parseInt($('#scrape-max-pages').value, 10),
                chunk_size: parseInt($('#scrape-chunk-size').value, 10),
                webhook_url: $('#scrape-webhook').value || null,
            };
            try {
                const endpoint = $('#scrape-ai').checked ? '/scrape-dynamic' : '/scrape';
                const data = await apiPost(endpoint, body);
                showResult(resultBox, true, `Görev kuyruğa eklendi: ${toText(data.task_id)}`);
                addRecentTask(data.task_id, 'Katalog kazıma');
                showToast('Başarılı', 'Kazıma görevi kuyruğa eklendi.', 'success');
            } catch (err) {
                const msg = errorText(err);
                showResult(resultBox, false, msg);
                showToast(isAuthError(err) ? 'Yetkisiz' : 'Hata', msg, 'error');
            }
        });

        $('#scrape-one-form').addEventListener('submit', async (e) => {
            e.preventDefault();
            const resultBox = $('#scrape-one-result');
            const body = {
                url: $('#scrape-one-url').value,
                force: $('#scrape-one-force').checked,
            };
            try {
                const data = await apiPost('/scrape-one', body);
                showResult(resultBox, true, `Görev kuyruğa eklendi: ${toText(data.task_id)}`);
                addRecentTask(data.task_id, 'Tek URL kazıma');
                showToast('Başarılı', 'Tek URL kazıma görevi eklendi.', 'success');
            } catch (err) {
                const msg = errorText(err);
                showResult(resultBox, false, msg);
                showToast(isAuthError(err) ? 'Yetkisiz' : 'Hata', msg, 'error');
            }
        });
    }

    // Tasks
    function initTasks() {
        $('#task-check-btn').addEventListener('click', async () => {
            const taskId = $('#task-id-input').value.trim();
            if (!taskId) return;
            const out = $('#task-result');
            out.classList.remove('hidden');
            if (taskId === '.' || taskId === '..') {
                out.textContent = 'Hata: Geçersiz görev kimliği.';
                return;
            }
            out.textContent = 'Sorgulanıyor...';
            try {
                const data = await apiGet(`/status/${encodeURIComponent(taskId)}`);
                out.textContent = JSON.stringify(data, null, 2);
            } catch (err) {
                out.textContent = errorText(err);
            }
        });
    }

    function addRecentTask(taskId, type) {
        const container = $('#recent-tasks');
        if (container.classList.contains('empty-state')) {
            container.classList.remove('empty-state');
            container.replaceChildren();
        }
        const item = el('div', { className: 'task-item' }, [
            el('div', { className: 'task-type', text: type }),
            el('code', { text: taskId }),
        ]);
        container.prepend(item);
    }

    // Books
    function initBooks() {
        $('#books-prev').addEventListener('click', () => {
            if (currentBooksOffset >= BOOKS_LIMIT) {
                currentBooksOffset -= BOOKS_LIMIT;
                loadBooks(currentBooksOffset);
            }
        });
        $('#books-next').addEventListener('click', () => {
            currentBooksOffset += BOOKS_LIMIT;
            loadBooks(currentBooksOffset);
        });
    }

    function messageRow(text) {
        return el('tr', {}, [el('td', { className: 'text-center', text, attrs: { colspan: 5 } })]);
    }

    function formatPrice(price) {
        if (price === null || price === undefined || price === '') return '£0.00';
        const n = typeof price === 'number' ? price : Number(price);
        return Number.isFinite(n) ? `£${n.toFixed(2)}` : '£0.00';
    }

    function ratingStars(rating) {
        const n = Math.trunc(Number(rating));
        const stars = Number.isFinite(n) ? Math.min(5, Math.max(0, n)) : 0;
        return '★'.repeat(stars) + '☆'.repeat(5 - stars);
    }

    function urlCell(rawUrl) {
        const text = toText(rawUrl);
        const shortUrl = text.length > 50 ? text.slice(0, 50) + '...' : text;
        const href = safeHttpUrl(rawUrl);
        if (!href) {
            // http/https dışı (javascript:, data:, göreli vb.) URL'ler tıklanamaz düz metin olarak gösterilir.
            return el('td', { text: shortUrl || '-' });
        }
        const link = el('a', { text: shortUrl, attrs: { target: '_blank', rel: 'noopener noreferrer' } });
        link.href = href;
        return el('td', {}, [link]);
    }

    function bookRow(b) {
        const book = b && typeof b === 'object' ? b : {};
        return el('tr', {}, [
            el('td', { text: book.title }),
            el('td', { text: formatPrice(book.price) }),
            el('td', { text: toText(book.availability) || '-' }),
            el('td', { text: ratingStars(book.rating) }),
            urlCell(book.url),
        ]);
    }

    async function loadBooks(offset) {
        const tbody = $('#books-tbody');
        tbody.replaceChildren(messageRow('Yükleniyor...'));
        try {
            const data = await apiGet(`/books?limit=${BOOKS_LIMIT}&offset=${offset}`);
            const total = Number(data.total);
            const pageNum = Math.floor(offset / BOOKS_LIMIT) + 1;
            $('#books-page-info').textContent = `Sayfa ${pageNum} (${Number.isFinite(total) ? total : '?'} kayıt)`;
            $('#books-prev').disabled = offset === 0;
            $('#books-next').disabled = !Number.isFinite(total) || offset + BOOKS_LIMIT >= total;

            const books = Array.isArray(data.books) ? data.books : [];
            if (books.length === 0) {
                tbody.replaceChildren(messageRow('Kayıt bulunamadı.'));
                return;
            }
            tbody.replaceChildren(...books.map(bookRow));
        } catch (err) {
            $('#books-prev').disabled = offset === 0;
            $('#books-next').disabled = true;
            tbody.replaceChildren(messageRow(errorText(err)));
        }
    }

    // Dead letter
    async function loadDeadLetter() {
        const out = $('#deadletter-output');
        out.textContent = 'Yükleniyor...';
        try {
            const data = await apiGet('/dead-letter');
            out.textContent = JSON.stringify(data, null, 2);
        } catch (err) {
            out.textContent = errorText(err);
        }
    }

    // Health page
    async function loadHealth() {
        const out = $('#health-output');
        out.textContent = 'Yükleniyor...';
        try {
            const data = await fetchHealth();
            out.textContent = JSON.stringify(data, null, 2);
        } catch (err) {
            out.textContent = errorText(err);
        }
    }

    // API key
    function initApiKey() {
        const saved = readStoredKey();
        if (saved) $('#api-key').value = saved;

        $('#save-key').addEventListener('click', () => {
            const key = $('#api-key').value.trim();
            const hadKey = Boolean(readStoredKey());
            if (!key && !hadKey) {
                showToast('API anahtarı boş', 'Kaydetmek için önce bir anahtar girin.', 'error');
                $('#api-key').focus();
                return;
            }
            setApiKey(key);
            hideAuthRequired();
            showToast(
                key ? 'Kaydedildi' : 'Silindi',
                key ? 'API key yerel olarak saklandı.' : 'Kayıtlı API key silindi.',
                'success',
            );
            // Yeni anahtarla geçerli sayfayı yeniden yükle.
            showPage(currentPageId());
        });

        $('#api-key').addEventListener('keydown', (e) => {
            if (e.key === 'Enter') $('#save-key').click();
        });

        $('#auth-banner-focus').addEventListener('click', () => {
            $('#api-key').focus();
        });
    }

    // Refresh
    function initRefresh() {
        $('#refresh-btn').addEventListener('click', () => {
            showPage(currentPageId());
            showToast('Yenilendi', 'Sayfa içeriği güncellendi.', 'info');
        });
    }

    // Init
    document.addEventListener('DOMContentLoaded', () => {
        initApiKey();
        initScrapeForms();
        initTasks();
        initBooks();
        initRefresh();
        // initNavigation, geçerli hash'e göre sayfayı (dashboard dahil) yükler.
        initNavigation();
    });
})();
