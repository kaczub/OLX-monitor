"""
OLX Monitor Bot
===============

Aplikacja monitoruje wybrane adresy URL serwisu OLX, wykrywa nowe
ogloszenia i wysyla powiadomienia przez Telegram.

Funkcje:
- Skanowanie OLX z uzyciem curl_cffi (impersonate="chrome120"),
  co pozwala uniknac blokad Cloudflare.
- Nowoczesny panel administracyjny (Modern Dark / Glassmorphism)
  pod adresem http://127.0.0.1:5000
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

STOP_EVENT = threading.Event()      # sygnal zatrzymania watku skanera
SCAN_NOW_EVENT = threading.Event()  # sygnal natychmiastowego skanu
SCANNER_ALIVE = threading.Event()   # informacja, czy skaner pracuje
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
        SCANNER_ALIVE.set()
        log.info("Skaner uruchomiony.")
        try:
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
        finally:
            SCANNER_ALIVE.clear()
            log.info("Skaner zatrzymany.")

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
# Panel WWW (Flask) - Modern Dark / Glassmorphism
# ---------------------------------------------------------------------------

app = Flask(__name__)

PANEL_HTML = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OLX Monitor Bot</title>
<style>
  :root {
    --bg: #0f172a;
    --card: #1e293b;
    --border: #334155;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --emerald: #10b981;
    --cyan: #06b6d4;
    --indigo: #6366f1;
    --rose: #f43f5e;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    min-height: 100vh;
    background-color: var(--bg);
    background-image:
      radial-gradient(900px 520px at 10% -12%, rgba(16,185,129,.14), transparent 60%),
      radial-gradient(900px 520px at 90% -6%, rgba(99,102,241,.16), transparent 60%),
      radial-gradient(760px 520px at 50% 112%, rgba(6,182,212,.10), transparent 60%);
    background-attachment: fixed;
    color: var(--text);
    font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .page { max-width: 1240px; margin: 0 auto; padding: 28px 20px 44px; }

  /* Naglowek */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; flex-wrap: wrap; margin-bottom: 22px;
  }
  .brand { display: flex; align-items: center; gap: 14px; }
  .logo {
    width: 46px; height: 46px; border-radius: 14px; display: grid; place-items: center;
    background: linear-gradient(135deg, rgba(16,185,129,.95), rgba(6,182,212,.85));
    box-shadow: 0 10px 26px -8px rgba(16,185,129,.55);
  }
  .logo svg { width: 24px; height: 24px; }
  .brand h1 { font-size: 20px; margin: 0; letter-spacing: .2px; }
  .brand p { margin: 3px 0 0; font-size: 12.5px; color: var(--muted); }

  .pill {
    display: inline-flex; align-items: center; gap: 9px; padding: 9px 15px;
    border-radius: 999px; font-size: 13px; font-weight: 600;
    border: 1px solid var(--border); background: rgba(30,41,59,.72);
    backdrop-filter: blur(10px); transition: color .2s, border-color .2s;
  }
  .pill .dot { width: 9px; height: 9px; border-radius: 50%; }
  .pill.emerald { color: #6ee7b7; border-color: rgba(16,185,129,.45); }
  .pill.emerald .dot { background: #10b981; box-shadow: 0 0 0 4px rgba(16,185,129,.16); }
  .pill.cyan { color: #67e8f9; border-color: rgba(6,182,212,.5); }
  .pill.cyan .dot { background: #06b6d4; animation: pulse 1.15s ease-in-out infinite; }
  .pill.rose { color: #fda4af; border-color: rgba(244,63,94,.5); }
  .pill.rose .dot { background: #f43f5e; box-shadow: 0 0 0 4px rgba(244,63,94,.16); }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: .35; transform: scale(.7); }
  }

  /* Uklad */
  .grid {
    display: grid; gap: 20px; align-items: start;
    grid-template-columns: minmax(0, 1.12fr) minmax(0, .88fr);
  }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: rgba(30,41,59,.72);
    border: 1px solid var(--border);
    border-radius: 12px;
    backdrop-filter: blur(14px);
    box-shadow: 0 18px 44px -26px rgba(0,0,0,.95);
    overflow: hidden;
  }
  .card + .card { margin-top: 20px; }
  .card-head {
    padding: 16px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }
  .card-head h2 {
    margin: 0; font-size: 13.5px; letter-spacing: .8px;
    text-transform: uppercase; color: #cbd5e1; font-weight: 700;
  }
  .card-body { padding: 20px; }

  /* Formularz */
  .field { margin-bottom: 16px; }
  .field:last-child { margin-bottom: 0; }
  .label {
    display: block; font-size: 12.5px; font-weight: 600;
    color: var(--muted); margin-bottom: 7px; letter-spacing: .2px;
  }
  .input, .select, .textarea {
    width: 100%; padding: 11px 13px; border-radius: 10px;
    background: #0f172a; color: var(--text);
    border: 1px solid var(--border);
    font-size: 13.5px; font-family: inherit; outline: none;
    transition: border-color .15s, box-shadow .15s, background .15s;
  }
  .textarea { resize: vertical; min-height: 138px; line-height: 1.5; }
  .input:focus, .select:focus, .textarea:focus {
    border-color: var(--emerald);
    box-shadow: 0 0 0 3px rgba(16,185,129,.18);
    background: #0b1220;
  }
  .mono { font-family: "JetBrains Mono", Consolas, "SF Mono", Menlo, monospace; }
  .hint { margin-top: 6px; font-size: 11.5px; color: #64748b; }
  .row { display: grid; gap: 16px; }
  .row-2 { grid-template-columns: 1fr 1fr; }
  .row-3 { grid-template-columns: repeat(3, 1fr); }
  @media (max-width: 620px) { .row-2, .row-3 { grid-template-columns: 1fr; } }

  .check {
    display: flex; align-items: flex-start; gap: 10px; padding: 11px 13px;
    border: 1px solid var(--border); border-radius: 10px;
    background: rgba(15,23,42,.6); margin-bottom: 10px; cursor: pointer;
  }
  .check:last-of-type { margin-bottom: 0; }
  .check input { width: 17px; height: 17px; margin-top: 1px; accent-color: var(--emerald); cursor: pointer; }
  .check span { font-size: 13px; color: #cbd5e1; }

  /* Przyciski */
  .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 4px; }
  .btn {
    border: none; border-radius: 10px; padding: 11px 18px;
    font-size: 13.5px; font-weight: 600; color: #fff; cursor: pointer;
    font-family: inherit; transition: transform .12s, box-shadow .15s, filter .15s;
  }
  .btn:hover { transform: translateY(-1px); filter: brightness(1.07); }
  .btn:active { transform: translateY(0); }
  .btn-cyan { background: linear-gradient(135deg, #06b6d4, #0891b2); box-shadow: 0 10px 24px -12px rgba(6,182,212,.9); }
  .btn-indigo { background: linear-gradient(135deg, #6366f1, #4f46e5); box-shadow: 0 10px 24px -12px rgba(99,102,241,.9); }
  .btn-emerald { background: linear-gradient(135deg, #10b981, #059669); box-shadow: 0 10px 24px -12px rgba(16,185,129,.9); }

  .feedback {
    margin-top: 14px; padding: 12px 14px; border-radius: 10px;
    font-size: 13px; display: none; border: 1px solid transparent;
  }
  .feedback.show { display: block; }
  .feedback.ok { background: rgba(16,185,129,.12); border-color: rgba(16,185,129,.4); color: #6ee7b7; }
  .feedback.err { background: rgba(244,63,94,.12); border-color: rgba(244,63,94,.4); color: #fda4af; }
  .feedback.info { background: rgba(6,182,212,.12); border-color: rgba(6,182,212,.4); color: #67e8f9; }

  /* Status */
  .stats { display: flex; flex-direction: column; }
  .stat {
    display: flex; align-items: center; justify-content: space-between; gap: 12px;
    padding: 10px 0; border-bottom: 1px dashed rgba(51,65,85,.75); font-size: 13px;
  }
  .stat:last-child { border-bottom: none; }
  .stat .k { color: var(--muted); }
  .stat .v { font-weight: 600; text-align: right; }
  .stat .v.muted { color: #64748b; font-weight: 500; }
  .stat .v.warn { color: #fcd34d; font-weight: 500; }
  .badge {
    font-size: 11.5px; font-weight: 700; padding: 4px 10px;
    border-radius: 999px; letter-spacing: .3px;
  }
  .badge.ok { background: rgba(16,185,129,.15); color: #6ee7b7; border: 1px solid rgba(16,185,129,.4); }
  .badge.off { background: rgba(148,163,184,.12); color: #cbd5e1; border: 1px solid rgba(148,163,184,.3); }

  /* Terminal logow */
  .terminal {
    background: #0b1120; border: 1px solid var(--border); border-radius: 10px;
    padding: 14px; height: 340px; overflow-y: auto;
    font-family: "JetBrains Mono", Consolas, "SF Mono", Menlo, monospace;
    font-size: 12px; line-height: 1.7;
  }
  .terminal .ln { white-space: pre-wrap; word-break: break-word; color: #94a3b8; }
  .terminal .ln.ok { color: #6ee7b7; }
  .terminal .ln.warn { color: #fcd34d; }
  .terminal .ln.error { color: #fda4af; }
  .terminal::-webkit-scrollbar { width: 9px; }
  .terminal::-webkit-scrollbar-thumb { background: #334155; border-radius: 6px; }
  .terminal::-webkit-scrollbar-track { background: transparent; }

  .live { display: inline-flex; align-items: center; gap: 7px; font-size: 11px; color: #64748b; text-transform: none; letter-spacing: 0; }
  .live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--emerald); animation: pulse 1.6s infinite; }

  footer { margin-top: 22px; font-size: 12px; color: #64748b; }
  footer code { color: #94a3b8; }
</style>
</head>
<body>
<div class="page">

  <div class="topbar">
    <div class="brand">
      <div class="logo">
        <svg viewBox="0 0 24 24" fill="none" stroke="#0b1220" stroke-width="2.2"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="9"></circle>
          <circle cx="12" cy="12" r="4.4"></circle>
          <path d="M12 12 19 5"></path>
        </svg>
      </div>
      <div>
        <h1>OLX Monitor Bot</h1>
        <p>Panel dziala lokalnie (127.0.0.1) &middot; nie zamykaj tego okna</p>
      </div>
    </div>
    <div class="pill emerald" id="status-pill">
      <span class="dot"></span><span id="status-text">Oczekuje</span>
    </div>
  </div>

  <div class="grid">

    <!-- LEWA KOLUMNA: KONFIGURACJA -->
    <section>
      <div class="card">
        <div class="card-head"><h2>Konfiguracja</h2></div>
        <div class="card-body">
          <form method="post" action="/save">
            <div class="row row-2">
              <div class="field">
                <label class="label" for="f-token">Token bota Telegram</label>
                <input id="f-token" class="input mono" type="text" name="telegram_bot_token"
                       value="{{ cfg.telegram_bot_token }}" placeholder="123456789:AAF...">
              </div>
              <div class="field">
                <label class="label" for="f-chat">Chat ID</label>
                <input id="f-chat" class="input mono" type="text" name="telegram_chat_id"
                       value="{{ cfg.telegram_chat_id }}" placeholder="123456789">
              </div>
            </div>

            <div class="field">
              <label class="label" for="f-urls">Adresy URL z OLX (jeden na linie)</label>
              <textarea id="f-urls" class="textarea mono" name="urls"
                        placeholder="https://www.olx.pl/elektronika/q-iphone/?search%5Border%5D=created_at%3Adesc">{{ cfg.urls | join('\n') }}</textarea>
              <div class="hint">Wklej linki skopiowane z OLX wraz z filtrami (cena, promien, sortowanie od najnowszych).</div>
            </div>

            <div class="row row-2">
              <div class="field">
                <label class="label" for="f-keywords">Slowa kluczowe (opcjonalnie, po przecinku)</label>
                <input id="f-keywords" class="input" type="text" name="keywords"
                       value="{{ cfg.keywords }}" placeholder="np. iphone, nieuszkodzony">
              </div>
              <div class="field">
                <label class="label" for="f-mode">Tryb filtra slow kluczowych</label>
                <select id="f-mode" class="select" name="keyword_mode">
                  <option value="off" {% if cfg.keyword_mode == 'off' %}selected{% endif %}>Wylaczony</option>
                  <option value="include" {% if cfg.keyword_mode == 'include' %}selected{% endif %}>Zawiera (tylko oferty z tym slowem)</option>
                  <option value="exclude" {% if cfg.keyword_mode == 'exclude' %}selected{% endif %}>Nie zawiera (pomija oferty z tym slowem)</option>
                </select>
              </div>
            </div>

            <div class="row row-3">
              <div class="field">
                <label class="label" for="f-min">Min. interwal (s)</label>
                <input id="f-min" class="input" type="number" name="min_interval" min="30"
                       value="{{ cfg.min_interval }}">
              </div>
              <div class="field">
                <label class="label" for="f-max">Maks. interwal (s)</label>
                <input id="f-max" class="input" type="number" name="max_interval" min="30"
                       value="{{ cfg.max_interval }}">
              </div>
              <div class="field">
                <label class="label" for="f-pages">Liczba stron</label>
                <input id="f-pages" class="input" type="number" name="max_pages" min="1" max="10"
                       value="{{ cfg.max_pages }}">
              </div>
            </div>

            <div class="field">
              <label class="check">
                <input type="checkbox" name="telegram_enabled" {% if cfg.telegram_enabled %}checked{% endif %}>
                <span>Wysylaj powiadomienia Telegram</span>
              </label>
              <label class="check">
                <input type="checkbox" name="notify_first_scan" {% if cfg.notify_first_scan %}checked{% endif %}>
                <span>Powiadamiaj o wszystkich ofertach z pierwszego skanu</span>
              </label>
            </div>

            <div class="actions">
              <button type="submit" class="btn btn-cyan">Zapisz konfiguracje</button>
              <button type="button" class="btn btn-indigo" onclick="postAction('/test')">Test wiadomosci Telegram</button>
              <button type="button" class="btn btn-emerald" onclick="postAction('/scan_now')">Skanuj teraz</button>
            </div>

            <div id="action-feedback" class="feedback"></div>
          </form>
        </div>
      </div>
    </section>

    <!-- PRAWA KOLUMNA: STATUS I LOGI -->
    <section>
      <div class="card">
        <div class="card-head"><h2>Status</h2></div>
        <div class="card-body">
          <div class="stats">
            <div class="stat"><span class="k">Ostatni skan</span><span class="v muted" id="st-last-scan">-</span></div>
            <div class="stat"><span class="k">Nowe oferty w ostatnim skanie</span><span class="v" id="st-last-found">-</span></div>
            <div class="stat"><span class="k">Nastepny skan za</span><span class="v mono" id="st-next">-</span></div>
            <div class="stat"><span class="k">Zapamietane oferty</span><span class="v" id="st-seen">-</span></div>
            <div class="stat"><span class="k">Wyslane powiadomienia</span><span class="v" id="st-sent">-</span></div>
            <div class="stat"><span class="k">Liczba adresow URL</span><span class="v" id="st-urls">-</span></div>
            <div class="stat"><span class="k">Status Telegrama</span><span class="v"><span id="st-tg" class="badge off">-</span></span></div>
            <div class="stat"><span class="k">Czas dzialania</span><span class="v mono" id="st-uptime">-</span></div>
            <div class="stat"><span class="k">Ostatni blad</span><span class="v muted" id="st-error">-</span></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>Dziennik zdarzen</h2>
          <span class="live"><span class="dot"></span>na zywo</span>
        </div>
        <div class="card-body">
          <div id="log-box" class="terminal">Ladowanie logu...</div>
        </div>
      </div>
    </section>

  </div>

  <footer>
    Historia ofert: <code>seen_offers.json</code> &middot;
    Konfiguracja: <code>config.json</code> &middot;
    Log: <code>bot.log</code>
  </footer>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const SAVED = {{ 'true' if saved else 'false' }};

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

  function setStatus(d) {
    const pill = $('status-pill');
    const text = $('status-text');
    let cls = 'emerald';
    let label = 'Oczekuje';
    if (!d.scanner_running) {
      cls = 'rose';
      label = 'Zatrzymany';
    } else if (d.scanning_now) {
      cls = 'cyan';
      label = 'Skanowanie...';
    }
    pill.className = 'pill ' + cls;
    text.textContent = label;
  }

  function renderLogs(lines) {
    const box = $('log-box');
    const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 48;
    box.innerHTML = '';
    lines.forEach(function (line) {
      const el = document.createElement('div');
      el.className = 'ln';
      if (line.indexOf('| ERROR |') !== -1) el.classList.add('error');
      else if (line.indexOf('| WARNING |') !== -1) el.classList.add('warn');
      else if (line.indexOf('| INFO |') !== -1) el.classList.add('ok');
      el.textContent = line;
      box.appendChild(el);
    });
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  async function refresh() {
    try {
      const r = await fetch('/api/status');
      const d = await r.json();
      setStatus(d);
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
      tg.className = 'badge ' + (d.telegram_configured ? 'ok' : 'off');
      renderLogs(d.logs);
    } catch (e) { /* serwer chwilowo niedostepny */ }
  }

  function feedback(message, kind) {
    const box = $('action-feedback');
    box.className = 'feedback show ' + kind;
    box.textContent = message;
  }

  async function postAction(url) {
    feedback('Wykonywanie...', 'info');
    try {
      const r = await fetch(url, { method: 'POST' });
      const d = await r.json();
      feedback(d.message, d.ok ? 'ok' : 'err');
    } catch (e) {
      feedback('Blad polaczenia z serwerem.', 'err');
    }
  }

  if (SAVED) feedback('Konfiguracja zostala zapisana.', 'ok');

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
        scanner_running=SCANNER_ALIVE.is_set(),
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
