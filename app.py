"""
OLX Monitor Bot
===============

Aplikacja monitoruje wybrane adresy URL serwisu OLX, wykrywa nowe
ogloszenia i wysyla powiadomienia przez Telegram.

Funkcje:
- Skanowanie OLX z uzyciem curl_cffi (impersonate="chrome120"),
  co pozwala uniknac blokad Cloudflare.
- Panel administracyjny (Flask + Bootstrap, ciemny motyw) pod adresem
  http://127.0.0.1:5000
- Losowe interwaly miedzy skanowaniami (domyslnie 150-300 s).
- Historia widzianych ofert w pliku seen_offers.json (bez duplikatow).
- Konfiguracja zapisywana w pliku config.json.
- Opcjonalne filtry slow kluczowych w tytulach ofert.

Zgodnosc:
- Jesli curl_cffi nie jest dostepne (np. Android / Termux, gdzie nie ma
  gotowych paczek), program automatycznie korzysta z biblioteki requests.

Uruchomienie:  python app.py
"""

import html
import json
import logging
import os
import random
import re
import threading
import time
from collections import deque
from datetime import datetime
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

try:
    import curl_cffi.requests as http_client

    HAS_CURL_CFFI = True
except ImportError:  # np. Android / Termux - brak gotowych paczek curl_cffi
    import requests as http_client

    HAS_CURL_CFFI = False

from flask import Flask, jsonify, redirect, render_template_string, request

# ---------------------------------------------------------------------------
# Stale i sciezki
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_offers.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

MAX_SEEN_OFFERS = 5000          # maksymalna liczba zapamietanych ofert
MIN_INTERVAL = 30               # minimalny dozwolony interwal (sekundy)
BROWSER_IMITATE = "chrome120"   # profil przegladarki dla curl_cffi

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    "telegram_enabled": True,
    "urls": [],
    "keywords": "",
    "keyword_mode": "off",       # "off" | "include" | "exclude"
    "min_interval": 150,
    "max_interval": 300,
    "max_pages": 2,
    "notify_first_scan": False,
}

LOG_LINES = deque(maxlen=300)    # ostatnie linie logu (wyswietlane w panelu)

STATUS = {
    "started_at": time.time(),
    "scanning_now": False,
    "last_scan": None,
    "last_scan_found": 0,
    "next_scan_in": None,
    "sent_total": 0,
    "last_error": None,
}

STOP_EVENT = threading.Event()     # sygnal zatrzymania watku skanera
SCAN_NOW_EVENT = threading.Event() # sygnal natychmiastowego skanu
CONFIG_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()

log = logging.getLogger("olxbot")

# ---------------------------------------------------------------------------
# Logowanie
# ---------------------------------------------------------------------------


class DequeHandler(logging.Handler):
    """Handler logowania zapisujacy komunikaty do kolejki (na potrzeby GUI)."""

    ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

    def __init__(self, target):
        super().__init__()
        self.target = target

    def emit(self, record):
        try:
            self.target.append(self.ANSI_RE.sub("", self.format(record)))
        except Exception:
            pass


def setup_logging():
    """Konfiguruje logowanie: konsola + plik bot.log + kolejka dla panelu."""
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()

    # Ograniczamy gadatliwosc serwera Flask (zapytania HTTP nie zasmiecaja logu)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        log.warning("Nie mozna zapisac logu do pliku: %s", exc)

    deque_handler = DequeHandler(LOG_LINES)
    deque_handler.setFormatter(fmt)
    root.addHandler(deque_handler)


# ---------------------------------------------------------------------------
# Konfiguracja (config.json)
# ---------------------------------------------------------------------------


def load_config():
    """Wczytuje konfiguracje z pliku; w razie bledu zwraca wartosci domyslne."""
    cfg = dict(DEFAULT_CONFIG)
    if not os.path.exists(CONFIG_PATH):
        return cfg
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            cfg.update({k: v for k, v in data.items() if k in DEFAULT_CONFIG})
        return cfg
    except (OSError, ValueError) as exc:
        log.warning("Blad odczytu config.json (%s). Uzywam ustawien domyslnych.", exc)
        backup_corrupt_config()
        return cfg


def backup_corrupt_config():
    """Przenosi uszkodzony plik konfiguracyjny do config.json.bak."""
    try:
        if os.path.exists(CONFIG_PATH):
            os.replace(CONFIG_PATH, CONFIG_PATH + ".bak")
    except OSError:
        pass


def save_config(cfg):
    """Zapisuje konfiguracje do pliku config.json."""
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
            return True
        except OSError as exc:
            log.error("Nie mozna zapisac config.json: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Historia widzianych ofert (seen_offers.json)
# ---------------------------------------------------------------------------


def load_seen():
    """Wczytuje historie widzianych ofert oraz liste znanych adresow URL."""
    default = {"offers": {}, "known_urls": {}}
    if not os.path.exists(SEEN_PATH):
        return default
    try:
        with open(SEEN_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return default
        if "offers" not in data:
            data = {"offers": data, "known_urls": {}}
        data.setdefault("offers", {})
        data.setdefault("known_urls", {})
        return data
    except (OSError, ValueError) as exc:
        log.warning("Blad odczytu seen_offers.json (%s). Zaczynam od pustej historii.", exc)
        return default


def save_seen(data):
    """Zapisuje historie widzianych ofert (z ograniczeniem rozmiaru)."""
    offers = data.get("offers", {})
    if len(offers) > MAX_SEEN_OFFERS:
        oldest_first = sorted(offers.items(), key=lambda item: item[1])
        for key, _ in oldest_first[: len(offers) - MAX_SEEN_OFFERS]:
            offers.pop(key, None)
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Nie mozna zapisac seen_offers.json: %s", exc)


# ---------------------------------------------------------------------------
# Komunikacja HTTP (curl_cffi z profilem Chrome)
# ---------------------------------------------------------------------------


def http_request(method, url, **kwargs):
    """
    Wykonuje zadanie HTTP z udawaniem przegladarki Chrome.
    Jesli curl_cffi nie jest dostepne, korzysta z requests (bez imitacji TLS).
    """
    if not HAS_CURL_CFFI:
        return http_client.request(method, url, **kwargs)
    try:
        return http_client.request(method, url, impersonate=BROWSER_IMITATE, **kwargs)
    except (ValueError, TypeError):
        log.debug("Profil %s niedostepny - uzywam domyslnego profilu.", BROWSER_IMITATE)
        return http_client.request(method, url, **kwargs)


def fetch_html(page_url, timeout=30):
    """Pobiera strone OLX jako tekst HTML."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    response = http_request("GET", page_url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text


def looks_like_block(html_text):
    """Wykrywa strony blokad (captcha / Cloudflare)."""
    lowered = html_text.lower()
    return ("captcha" in lowered and len(html_text) < 200000) or "cf-chl-" in lowered


def describe_http_error(exc):
    """Zwraca czytelny opis bledu HTTP (z podpowiedzia dla uzytkownika)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 403:
        if HAS_CURL_CFFI:
            return ("OLX odrzucil zapytanie (403). Zmniejsz liczbe adresow URL "
                    "i zwieksz interwal skanowania.")
        return ("OLX odrzucil zapytanie (403). Tryb zgodnosci bez curl_cffi "
                "(np. Android/Termux) jest czesto blokowany - zalecany komputer lub VPS.")
    if status == 429:
        return "Zbyt wiele zapytan (429). Zwieksz interwal skanowania."
    if status == 504:
        return "OLX chwilowo nie odpowiada (504). Program ponowi probe automatycznie."
    return f"Blad sieci: {exc}"


# ---------------------------------------------------------------------------
# Parsowanie stron OLX
# ---------------------------------------------------------------------------


def set_page_param(url, page):
    """Ustawia parametr ?page=N w adresie URL, zachowujac pozostale parametry."""
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def extract_offers(html_text, page_url):
    """
    Wyciaga oferty z osadzonego JSON-a (window.__PRERENDERED_STATE__).
    Zwraca liste slownikow: id, title, price, location, url.
    """
    try:
        start = html_text.index("window.__PRERENDERED_STATE__")
        eq = html_text.index("=", start)
        decoder = json.JSONDecoder()
        first, _ = decoder.raw_decode(html_text[eq + 1:].lstrip())
        if isinstance(first, str):
            data = json.loads(first)      # stan jest zapisany jako JSON w stringu
        elif isinstance(first, dict):
            data = first
        else:
            return []
    except (ValueError, json.JSONDecodeError) as exc:
        log.debug("Nie udalo sie sparsowac danych strony: %s", exc)
        return []

    ads = None
    for path in (("listing", "listing", "ads"), ("listing", "ads"), ("ads",)):
        node = data
        for key in path:
            if isinstance(node, dict) and key in node:
                node = node[key]
            else:
                node = None
                break
        if isinstance(node, list):
            ads = node
            break
    if not ads:
        return []

    parsed = urlparse(page_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    offers = []

    for ad in ads:
        if not isinstance(ad, dict):
            continue
        offer_id = ad.get("id") or ad.get("offerId")
        title = (ad.get("title") or "").strip()
        if not title and not offer_id:
            continue

        price = ""
        price_info = ad.get("price")
        if isinstance(price_info, dict):
            if price_info.get("free"):
                price = "Za darmo"
            elif price_info.get("exchange"):
                price = "Zamiana"
            else:
                price = str(price_info.get("displayValue") or "")
        if not price:
            for param in ad.get("params") or []:
                if isinstance(param, dict) and param.get("key") == "price":
                    value = param.get("value")
                    if isinstance(value, dict):
                        price = value.get("value") or ""
                    elif value:
                        price = value
                    break

        location_parts = []
        location = ad.get("location")
        if isinstance(location, dict):
            for key in ("cityName", "regionName", "city", "region"):
                part = (location.get(key) or "").strip()
                if part:
                    location_parts.append(part)

        relative_url = (ad.get("url") or "").strip()
        offer_url = relative_url if relative_url.startswith("http") else urljoin(base_url + "/", relative_url)

        offers.append(
            {
                "id": offer_id or offer_url,
                "title": title or "Brak tytulu",
                "price": str(price),
                "location": ", ".join(location_parts),
                "url": offer_url,
            }
        )
    return offers


def keyword_ok(title, cfg):
    """
    Sprawdza, czy tytul przechodzi filtr slow kluczowych.
    tryby: off (wylaczony), include (musi zawierac), exclude (nie moze zawierac).
    """
    words = [
        w.strip().lower()
        for w in str(cfg.get("keywords") or "").split(",")
        if w.strip()
    ]
    mode = cfg.get("keyword_mode", "off")
    if mode == "off" or not words:
        return True
    lowered = (title or "").lower()
    if mode == "include":
        return any(word in lowered for word in words)
    if mode == "exclude":
        return not any(word in lowered for word in words)
    return True


# ---------------------------------------------------------------------------
# Powiadomienia Telegram
# ---------------------------------------------------------------------------


def build_offer_message(offer):
    """Buduje sformatowana wiadomosc HTML dla Telegrama."""
    title = html.escape(offer.get("title") or "Brak tytulu")
    price = html.escape(offer.get("price") or "Nie podano")
    location = html.escape(offer.get("location") or "Nie podano")
    offer_url = html.escape(offer.get("url") or "")
    return (
        "<b>Nowe ogloszenie na OLX!</b>\n\n"
        f"<b>Tytul:</b> {title}\n"
        f"<b>Cena:</b> {price}\n"
        f"<b>Lokalizacja:</b> {location}\n\n"
        f'<a href="{offer_url}">Zobacz ogloszenie</a>'
    )


def build_offer_keyboard(offer):
    """Buduje przycisk prowadzacy bezposrednio do oferty."""
    return {
        "inline_keyboard": [
            [{"text": "Zobacz oferte", "url": offer.get("url") or ""}]
        ]
    }


def send_telegram_message(cfg, text, reply_markup=None):
    """Wysyla wiadomosc przez API Telegrama."""
    token = (cfg.get("telegram_bot_token") or "").strip()
    chat_id = (cfg.get("telegram_chat_id") or "").strip()
    if not token or not chat_id:
        raise RuntimeError("Brak tokenu bota lub Chat ID w konfiguracji.")

    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)

    response = http_request(
        "POST",
        f"https://api.telegram.org/bot{token}/sendMessage",
        json=payload,
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description") or "Nieznany blad Telegram API.")
    return data


def send_offer_notification(cfg, offer):
    """Wysyla powiadomienie o nowej ofercie."""
    send_telegram_message(
        cfg,
        build_offer_message(offer),
        reply_markup=build_offer_keyboard(offer),
    )


# ---------------------------------------------------------------------------
# Skaner (watek w tle)
# ---------------------------------------------------------------------------


class ScannerThread(threading.Thread):
    """Watek cyklicznie skanujacy skonfigurowane adresy OLX."""

    def __init__(self):
        super().__init__(daemon=True, name="olx-scanner")

    def run(self):
        log.info("Skaner uruchomiony.")
        while not STOP_EVENT.is_set():
            cfg = load_config()
            if not cfg.get("urls"):
                log.info("Brak adresow URL w konfiguracji. Oczekiwanie 60 s...")
                deadline = time.time() + 60
                while time.time() < deadline and not STOP_EVENT.is_set():
                    if SCAN_NOW_EVENT.is_set():
                        SCAN_NOW_EVENT.clear()
                        break
                    STOP_EVENT.wait(5)
                continue
            try:
                self.scan_once(cfg)
            except Exception:
                log.exception("Nieoczekiwany blad podczas skanowania.")
            self._sleep_with_abort(cfg)

    def _sleep_with_abort(self, cfg):
        """Uspia watek na losowy czas, reagujac na zmiane konfiguracji."""
        try:
            min_interval = max(MIN_INTERVAL, int(cfg.get("min_interval", 150)))
            max_interval = max(min_interval, int(cfg.get("max_interval", 300)))
        except (TypeError, ValueError):
            min_interval, max_interval = 150, 300
        interval = random.uniform(min_interval, max_interval)
        deadline = time.time() + interval
        while time.time() < deadline and not STOP_EVENT.is_set():
            remaining = deadline - time.time()
            with STATUS_LOCK:
                STATUS["next_scan_in"] = int(remaining)
            if SCAN_NOW_EVENT.is_set():
                SCAN_NOW_EVENT.clear()
                with STATUS_LOCK:
                    STATUS["next_scan_in"] = 0
                return
            STOP_EVENT.wait(min(5.0, remaining))

    def scan_once(self, cfg):
        """Wykonuje pojedynczy pelny skan wszystkich adresow URL."""
        with STATUS_LOCK:
            STATUS["scanning_now"] = True
            STATUS["next_scan_in"] = None
            STATUS["last_error"] = None
        started = time.time()
        new_notifications = 0
        checked = 0
        seen = load_seen()

        for url in cfg.get("urls", []):
            host = urlparse(url).netloc or "unknown"
            max_pages = max(1, int(cfg.get("max_pages", 1) or 1))
            for page in range(1, max_pages + 1):
                page_url = set_page_param(url, page)
                try:
                    page_html = fetch_html(page_url)
                except Exception as exc:
                    message = describe_http_error(exc)
                    with STATUS_LOCK:
                        STATUS["last_error"] = message
                    log.warning("Nie udalo sie pobrac %s (%s)", page_url, message)
                    break

                if looks_like_block(page_html):
                    with STATUS_LOCK:
                        STATUS["last_error"] = "OLX zablokowal zapytanie (Cloudflare/captcha)."
                    log.warning("Prawdopodobna blokada Cloudflare dla %s", page_url)
                    break

                offers = extract_offers(page_html, page_url)
                checked += len(offers)
                if not offers:
                    break

                for offer in offers:
                    key = f"{host}|{offer['id']}"
                    is_new = key not in seen["offers"]
                    seen["offers"][key] = time.time()
                    if not is_new:
                        continue
                    if not keyword_ok(offer["title"], cfg):
                        continue
                    url_known = url in seen["known_urls"]
                    if not (cfg.get("notify_first_scan") or url_known):
                        continue
                    if not (cfg.get("telegram_enabled")
                            and cfg.get("telegram_bot_token")
                            and cfg.get("telegram_chat_id")):
                        continue
                    try:
                        send_offer_notification(cfg, offer)
                        with STATUS_LOCK:
                            STATUS["sent_total"] += 1
                        new_notifications += 1
                        log.info("Wyslano powiadomienie: %s", offer["title"][:80])
                    except Exception as exc:
                        with STATUS_LOCK:
                            STATUS["last_error"] = f"Telegram: {exc}"
                        log.error("Blad wysylania do Telegrama: %s", exc)

                seen["known_urls"][url] = time.time()
                if page < max_pages:
                    time.sleep(random.uniform(2, 4))

        save_seen(seen)
        elapsed = time.time() - started
        with STATUS_LOCK:
            STATUS["scanning_now"] = False
            STATUS["last_scan"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            STATUS["last_scan_found"] = new_notifications
        log.info(
            "Skan zakonczony: sprawdzono %d ofert, nowych powiadomien: %d (czas: %.1f s)",
            checked,
            new_notifications,
            elapsed,
        )


# ---------------------------------------------------------------------------
# Panel WWW (Flask)
# ---------------------------------------------------------------------------

app = Flask(__name__)

PANEL_HTML = """<!doctype html>
<html lang="pl" data-bs-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OLX Monitor Bot - Panel</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
  body { background-color: #11141b; }
  .card { background-color: #1a1f2b; border: 1px solid #2a3040; }
  .form-control, .form-select { background-color: #12161f; border-color: #2a3040; color: #e6e6e6; }
  #log-box { background-color: #0c0f15; color: #9fe8a0; font-size: 0.78rem;
             height: 320px; overflow-y: auto; padding: 10px; border-radius: 6px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  h1 .badge { font-size: 0.9rem; vertical-align: middle; }
</style>
</head>
<body>
<div class="container py-4">

  <div class="d-flex align-items-center justify-content-between flex-wrap mb-4">
    <h1 class="mb-0">OLX Monitor Bot <span id="st-scanning" class="badge bg-secondary ms-1">-</span></h1>
    <span class="text-secondary small">Panel dziala lokalnie - nie zamykaj okna aplikacji</span>
  </div>

  {% if saved %}
  <div class="alert alert-success alert-dismissible fade show" role="alert">
    Konfiguracja zostala zapisana.
    <button type="button" class="btn-close" data-bs-dismiss="alert"></button>
  </div>
  {% endif %}

  <div class="row g-4">
    <div class="col-lg-7">
      <div class="card shadow-sm">
        <div class="card-header fw-semibold">Konfiguracja</div>
        <div class="card-body">
          <form method="post" action="/save">
            <div class="row g-3">
              <div class="col-md-8">
                <label class="form-label">Token bota Telegram</label>
                <input type="text" class="form-control mono" name="telegram_bot_token"
                       value="{{ cfg.telegram_bot_token }}" placeholder="123456789:AAF...">
              </div>
              <div class="col-md-4">
                <label class="form-label">Chat ID</label>
                <input type="text" class="form-control mono" name="telegram_chat_id"
                       value="{{ cfg.telegram_chat_id }}" placeholder="123456789">
              </div>
              <div class="col-12">
                <label class="form-label">Adresy URL z OLX (jeden na linie)</label>
                <textarea class="form-control mono" name="urls" rows="5"
                          placeholder="https://www.olx.pl/elektronika/q-iphone/?search%5Border%5D=created_at%3Adesc">{{ cfg.urls | join('\n') }}</textarea>
                <div class="form-text">Wklej linki skopiowane z OLX wraz z ustawionymi filtrami (cena, promien, sortowanie).</div>
              </div>
              <div class="col-md-6">
                <label class="form-label">Slowa kluczowe (opcjonalnie, po przecinku)</label>
                <input type="text" class="form-control" name="keywords" value="{{ cfg.keywords }}"
                       placeholder="np. iphone, nieuszkodzony">
              </div>
              <div class="col-md-6">
                <label class="form-label">Tryb filtra slow kluczowych</label>
                <select class="form-select" name="keyword_mode">
                  <option value="off" {% if cfg.keyword_mode == 'off' %}selected{% endif %}>Wylaczony</option>
                  <option value="include" {% if cfg.keyword_mode == 'include' %}selected{% endif %}>Tylko jesli tytul zawiera ktorekolwiek slowo</option>
                  <option value="exclude" {% if cfg.keyword_mode == 'exclude' %}selected{% endif %}>Pomin, jesli tytul zawiera ktorekolwiek slowo</option>
                </select>
              </div>
              <div class="col-md-4">
                <label class="form-label">Min. interwal skanowania (s)</label>
                <input type="number" class="form-control" name="min_interval" min="30"
                       value="{{ cfg.min_interval }}">
              </div>
              <div class="col-md-4">
                <label class="form-label">Maks. interwal skanowania (s)</label>
                <input type="number" class="form-control" name="max_interval" min="30"
                       value="{{ cfg.max_interval }}">
              </div>
              <div class="col-md-4">
                <label class="form-label">Liczba stron do sprawdzenia</label>
                <input type="number" class="form-control" name="max_pages" min="1" max="10"
                       value="{{ cfg.max_pages }}">
              </div>
              <div class="col-12">
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" name="telegram_enabled" id="chk-tg"
                         {% if cfg.telegram_enabled %}checked{% endif %}>
                  <label class="form-check-label" for="chk-tg">Wysylaj powiadomienia Telegram</label>
                </div>
                <div class="form-check">
                  <input class="form-check-input" type="checkbox" name="notify_first_scan" id="chk-first"
                         {% if cfg.notify_first_scan %}checked{% endif %}>
                  <label class="form-check-label" for="chk-first">Powiadamiaj o wszystkich ofertach z pierwszego skanu nowego adresu URL</label>
                </div>
              </div>
              <div class="col-12 d-flex flex-wrap gap-2">
                <button type="submit" class="btn btn-primary">Zapisz konfiguracje</button>
                <button type="button" class="btn btn-outline-info" onclick="postAction('/test')">Test wiadomosci Telegram</button>
                <button type="button" class="btn btn-success" onclick="postAction('/scan_now')">Skanuj teraz</button>
              </div>
              <div class="col-12">
                <div id="action-feedback" class="alert d-none mb-0"></div>
              </div>
            </div>
          </form>
        </div>
      </div>
    </div>

    <div class="col-lg-5">
      <div class="card shadow-sm mb-4">
        <div class="card-header fw-semibold">Status</div>
        <div class="card-body">
          <table class="table table-sm table-borderless mb-0 align-middle">
            <tbody>
              <tr><td class="text-secondary">Ostatni skan</td><td class="text-end" id="st-last-scan">-</td></tr>
              <tr><td class="text-secondary">Nowe oferty w ostatnim skanie</td><td class="text-end" id="st-last-found">-</td></tr>
              <tr><td class="text-secondary">Nastepny skan za</td><td class="text-end mono" id="st-next">-</td></tr>
              <tr><td class="text-secondary">Zapamietane oferty</td><td class="text-end" id="st-seen">-</td></tr>
              <tr><td class="text-secondary">Wyslane powiadomienia</td><td class="text-end" id="st-sent">-</td></tr>
              <tr><td class="text-secondary">Adresy URL</td><td class="text-end" id="st-urls">-</td></tr>
              <tr><td class="text-secondary">Telegram</td><td class="text-end"><span id="st-tg" class="badge bg-secondary">-</span></td></tr>
              <tr><td class="text-secondary">Czas dzialania</td><td class="text-end mono" id="st-uptime">-</td></tr>
              <tr><td class="text-secondary">Ostatni blad</td><td class="text-end text-warning" id="st-error">-</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="card shadow-sm">
        <div class="card-header fw-semibold">Dziennik zdarzen (log)</div>
        <div class="card-body p-2">
          <pre id="log-box" class="mono mb-0">Ladowanie logu...</pre>
        </div>
      </div>
    </div>
  </div>

  <p class="text-secondary small mt-4 mb-0">
    Historia ofert: seen_offers.json &middot; Konfiguracja: config.json &middot; Log: bot.log
  </p>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script>
  const $ = (id) => document.getElementById(id);

  function fmtSecs(s) {
    if (s === null || s === undefined) return '-';
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const mm = String(m).padStart(2, '0');
    const ss = String(sec).padStart(2, '0');
    return h > 0 ? h + ':' + mm + ':' + ss : m + ':' + ss;
  }

  async function refresh() {
    try {
      const r = await fetch('/api/status');
      const d = await r.json();
      $('st-scanning').textContent = d.scanning_now ? 'Skanuje...' : 'Oczekuje';
      $('st-scanning').className = 'badge ms-1 ' + (d.scanning_now ? 'bg-warning text-dark' : 'bg-success');
      $('st-last-scan').textContent = d.last_scan || 'jeszcze nie bylo';
      $('st-last-found').textContent = d.last_scan_found;
      $('st-next').textContent = fmtSecs(d.next_scan_in);
      $('st-seen').textContent = d.seen_count;
      $('st-sent').textContent = d.sent_total;
      $('st-urls').textContent = d.urls_count;
      $('st-uptime').textContent = fmtSecs(d.uptime);
      $('st-error').textContent = d.last_error || 'brak';
      const tg = $('st-tg');
      tg.textContent = d.telegram_configured ? 'Skonfigurowany' : 'Brak danych';
      tg.className = 'badge ' + (d.telegram_configured ? 'bg-success' : 'bg-secondary');
      $('log-box').textContent = d.logs.join('\\n');
      $('log-box').scrollTop = $('log-box').scrollHeight;
    } catch (e) { /* serwer chwilowo niedostepny */ }
  }

  async function postAction(url) {
    const box = $('action-feedback');
    box.className = 'alert alert-info mb-0';
    box.textContent = 'Wykonywanie...';
    try {
      const r = await fetch(url, { method: 'POST' });
      const d = await r.json();
      box.className = 'alert mb-0 ' + (d.ok ? 'alert-success' : 'alert-danger');
      box.textContent = d.message;
    } catch (e) {
      box.className = 'alert alert-danger mb-0';
      box.textContent = 'Blad polaczenia z serwerem.';
    }
  }

  setInterval(refresh, 5000);
  refresh();
</script>
</body>
</html>
"""


def to_int(value, default, lo, hi):
    """Bezpieczna konwersja na int z obcieciem do przedzialu [lo, hi]."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


@app.get("/")
def index():
    """Glowna strona panelu."""
    cfg = load_config()
    saved = request.args.get("saved") == "1"
    return render_template_string(PANEL_HTML, cfg=cfg, saved=saved)


@app.post("/save")
def save():
    """Zapisuje konfiguracje przeslana z formularza."""
    cfg = load_config()
    cfg["telegram_bot_token"] = request.form.get("telegram_bot_token", "").strip()
    cfg["telegram_chat_id"] = request.form.get("telegram_chat_id", "").strip()
    cfg["urls"] = [
        line.strip()
        for line in (request.form.get("urls") or "").splitlines()
        if line.strip()
    ]
    cfg["keywords"] = request.form.get("keywords", "").strip()
    cfg["keyword_mode"] = request.form.get("keyword_mode", "off")
    if cfg["keyword_mode"] not in ("off", "include", "exclude"):
        cfg["keyword_mode"] = "off"
    cfg["min_interval"] = to_int(request.form.get("min_interval"), 150, MIN_INTERVAL, 86400)
    cfg["max_interval"] = to_int(request.form.get("max_interval"), 300, MIN_INTERVAL, 86400)
    if cfg["max_interval"] < cfg["min_interval"]:
        cfg["max_interval"] = cfg["min_interval"]
    cfg["max_pages"] = to_int(request.form.get("max_pages"), 2, 1, 10)
    cfg["telegram_enabled"] = request.form.get("telegram_enabled") == "on"
    cfg["notify_first_scan"] = request.form.get("notify_first_scan") == "on"

    if save_config(cfg):
        log.info(
            "Konfiguracja zapisana (URL: %d, interwal: %d-%d s, strony: %d).",
            len(cfg["urls"]),
            cfg["min_interval"],
            cfg["max_interval"],
            cfg["max_pages"],
        )
        return redirect("/?saved=1")
    return redirect("/?saved=0")


@app.post("/test")
def test_telegram():
    """Wysyla testowa wiadomosc do Telegrama."""
    cfg = load_config()
    if not (cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")):
        return jsonify(
            ok=False,
            message="Uzupelnij Token bota i Chat ID, zapisz konfiguracje "
                    "i wyslij /start do swojego bota w Telegramie.",
        )
    try:
        send_telegram_message(
            cfg,
            "<b>Test powiadomien OLX Monitor Bot</b>\n\n"
            "Polaczenie dziala poprawnie.",
        )
        log.info("Testowa wiadomosc Telegram wyslana pomyslnie.")
        return jsonify(ok=True, message="Wiadomosc testowa wyslana. Sprawdz Telegram.")
    except Exception as exc:
        log.error("Test Telegram nieudany: %s", exc)
        return jsonify(ok=False, message=f"Blad: {exc}")


@app.post("/scan_now")
def scan_now():
    """Wymusza natychmiastowy skan."""
    SCAN_NOW_EVENT.set()
    return jsonify(ok=True, message="Skan zostanie uruchomiony za chwile.")


@app.get("/api/status")
def api_status():
    """Zwraca status aplikacji oraz ostatnie linie logu (dla panelu)."""
    cfg = load_config()
    with STATUS_LOCK:
        status = dict(STATUS)
    return jsonify(
        scanning_now=status["scanning_now"],
        last_scan=status["last_scan"],
        last_scan_found=status["last_scan_found"],
        next_scan_in=status["next_scan_in"],
        sent_total=status["sent_total"],
        uptime=int(time.time() - status["started_at"]),
        last_error=status["last_error"],
        seen_count=len(load_seen().get("offers", {})),
        urls_count=len(cfg.get("urls", [])),
        telegram_configured=bool(cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")),
        logs=list(LOG_LINES)[-60:],
    )


@app.get("/favicon.ico")
def favicon():
    """Pusty favicon (unikanie wpisow 404 w logu)."""
    return "", 204


# ---------------------------------------------------------------------------
# Start aplikacji
# ---------------------------------------------------------------------------


def find_free_port(start=5000, count=6):
    """Znajduje pierwszy wolny port z zakresu 5000-5005."""
    from socket import socket

    for port in range(start, start + count):
        sock = socket()
        try:
            sock.bind(("127.0.0.1", port))
            sock.close()
            return port
        except OSError:
            sock.close()
            continue
    return start


def main():
    """Punkt wejscia aplikacji."""
    setup_logging()
    log.info("Uruchamianie OLX Monitor Bot...")
    if HAS_CURL_CFFI:
        log.info("Klient HTTP: curl_cffi (imitacja przegladarki %s).", BROWSER_IMITATE)
    else:
        log.warning(
            "Klient HTTP: requests (curl_cffi niedostepne - tryb zgodnosci, "
            "np. Android/Termux). OLX moze czesciej stosowac blokady."
        )

    scanner = ScannerThread()
    scanner.start()

    port = find_free_port()
    banner = [
        "============================================================",
        "  OLX Monitor Bot",
        f"  Panel administracyjny:  http://127.0.0.1:{port}",
        "  Aby zatrzymac aplikacje nacisnij Ctrl+C",
        "============================================================",
    ]
    print("\n".join(banner))

    try:
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
    finally:
        STOP_EVENT.set()
        log.info("Aplikacja zatrzymana.")


if __name__ == "__main__":
    main()
