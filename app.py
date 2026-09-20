"""
OLX Monitor Bot
===============

Aplikacja monitoruje wybrane adresy URL serwisu OLX, wykrywa nowe
ogłoszenia i wysyła powiadomienia przez Telegram.

Funkcje:
- Skanowanie OLX z użyciem curl_cffi (impersonate="chrome120"),
  co pozwala uniknąć blokad Cloudflare.
- Panel administracyjny (ciemny motyw, tryb Operate) pod adresem
  http://127.0.0.1:5000
- Losowe interwały między skanowaniami (domyślnie 150-300 s).
- Historia widzianych ofert w pliku seen_offers.json (bez duplikatów).
- Konfiguracja zapisywana w pliku config.json.
- Opcjonalne filtry słów kluczowych w tytułach ofert.

Zgodność:
- Jeśli curl_cffi nie jest dostępne (np. Android / Termux, gdzie nie ma
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
# Stałe i ścieżki
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SEEN_PATH = os.path.join(BASE_DIR, "seen_offers.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")

MAX_SEEN_OFFERS = 5000          # maksymalna liczba zapamiętanych ofert
MIN_INTERVAL = 30               # minimalny dozwolony interwał (sekundy)
BROWSER_IMITATE = "chrome120"   # profil przeglądarki dla curl_cffi

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

LOG_LINES = deque(maxlen=300)    # ostatnie linie logu (wyświetlane w panelu)

STATUS = {
    "started_at": time.time(),
    "scanning_now": False,
    "last_scan": None,
    "last_scan_found": 0,
    "next_scan_in": None,
    "sent_total": 0,
    "last_error": None,
}

STOP_EVENT = threading.Event()      # sygnał zatrzymania wątku skanera
SCAN_NOW_EVENT = threading.Event()  # sygnał natychmiastowego skanu
SCANNER_ALIVE = threading.Event()   # informacja, czy skaner pracuje
CONFIG_LOCK = threading.Lock()
STATUS_LOCK = threading.Lock()

log = logging.getLogger("olxbot")

# ---------------------------------------------------------------------------
# Logowanie
# ---------------------------------------------------------------------------


class DequeHandler(logging.Handler):
    """Handler logowania zapisujący komunikaty do kolejki (na potrzeby GUI)."""

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

    # Ograniczamy gadatliwość serwera Flask (zapytania HTTP nie zaśmiecają logu)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    try:
        file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError as exc:
        log.warning("Nie można zapisać logu do pliku: %s", exc)

    deque_handler = DequeHandler(LOG_LINES)
    deque_handler.setFormatter(fmt)
    root.addHandler(deque_handler)


# ---------------------------------------------------------------------------
# Konfiguracja (config.json)
# ---------------------------------------------------------------------------


def load_config():
    """Wczytuje konfigurację z pliku; w razie błędu zwraca wartości domyślne."""
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
        log.warning("Błąd odczytu config.json (%s). Używam ustawień domyślnych.", exc)
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
    """Zapisuje konfigurację do pliku config.json."""
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    with CONFIG_LOCK:
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(merged, fh, ensure_ascii=False, indent=2)
            return True
        except OSError as exc:
            log.error("Nie można zapisać config.json: %s", exc)
            return False


# ---------------------------------------------------------------------------
# Historia widzianych ofert (seen_offers.json)
# ---------------------------------------------------------------------------


def load_seen():
    """Wczytuje historię widzianych ofert oraz listę znanych adresów URL."""
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
        log.warning("Błąd odczytu seen_offers.json (%s). Zaczynam od pustej historii.", exc)
        return default


def save_seen(data):
    """Zapisuje historię widzianych ofert (z ograniczeniem rozmiaru)."""
    offers = data.get("offers", {})
    if len(offers) > MAX_SEEN_OFFERS:
        oldest_first = sorted(offers.items(), key=lambda item: item[1])
        for key, _ in oldest_first[: len(offers) - MAX_SEEN_OFFERS]:
            offers.pop(key, None)
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError as exc:
        log.error("Nie można zapisać seen_offers.json: %s", exc)


# ---------------------------------------------------------------------------
# Komunikacja HTTP (curl_cffi z profilem Chrome)
# ---------------------------------------------------------------------------


def http_request(method, url, **kwargs):
    """
    Wykonuje żądanie HTTP z udawaniem przeglądarki Chrome.
    Jeśli curl_cffi nie jest dostępne, korzysta z requests (bez imitacji TLS).
    """
    if not HAS_CURL_CFFI:
        return http_client.request(method, url, **kwargs)
    try:
        return http_client.request(method, url, impersonate=BROWSER_IMITATE, **kwargs)
    except (ValueError, TypeError):
        log.debug("Profil %s niedostępny - używam domyślnego profilu.", BROWSER_IMITATE)
        return http_client.request(method, url, **kwargs)


def fetch_html(page_url, timeout=30):
    """Pobiera stronę OLX jako tekst HTML."""
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
    """Zwraca czytelny opis błędu HTTP (z podpowiedzią dla użytkownika)."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 403:
        if HAS_CURL_CFFI:
            return ("OLX odrzucił zapytanie (403). Zmniejsz liczbę adresów URL "
                    "i zwiększ interwał skanowania.")
        return ("OLX odrzucił zapytanie (403). Tryb zgodności bez curl_cffi "
                "(np. Android/Termux) jest często blokowany — zalecany komputer lub VPS.")
    if status == 429:
        return "Zbyt wiele zapytań (429). Zwiększ interwał skanowania."
    if status == 504:
        return "OLX chwilowo nie odpowiada (504). Program ponowi próbę automatycznie."
    return f"Błąd sieci: {exc}"


def describe_telegram_error(exc):
    """Zamienia techniczny błąd Telegrama na zrozumiały komunikat."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    text = str(exc).lower()
    if status == 401 or "unauthorized" in text or "401" in text:
        return ("Nieprawidłowy token bota. Skopiuj token ponownie od @BotFather "
                "i zapisz konfigurację.")
    if status == 400 or "chat not found" in text:
        return ("Nieprawidłowy Chat ID lub bot nie został jeszcze uruchomiony. "
                "Wyślij /start do swojego bota i sprawdź Chat ID.")
    if status == 403 or "blocked" in text or "forbidden" in text:
        return "Bot został zablokowany — odblokuj go w Telegramie."
    if "timed out" in text or "timeout" in text:
        return "Przekroczono czas połączenia z Telegramem. Sprawdź połączenie z internetem."
    return f"Błąd Telegrama: {exc}"


# ---------------------------------------------------------------------------
# Parsowanie stron OLX
# ---------------------------------------------------------------------------


def set_page_param(url, page):
    """Ustawia parametr ?page=N w adresie URL, zachowując pozostałe parametry."""
    parts = urlparse(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    query["page"] = [str(page)]
    return urlunparse(parts._replace(query=urlencode(query, doseq=True)))


def extract_offers(html_text, page_url):
    """
    Wyciąga oferty z osadzonego JSON-a (window.__PRERENDERED_STATE__).
    Zwraca listę słowników: id, title, price, location, url.
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
        log.debug("Nie udało się sparsować danych strony: %s", exc)
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
                "title": title or "Brak tytułu",
                "price": str(price),
                "location": ", ".join(location_parts),
                "url": offer_url,
            }
        )
    return offers


def keyword_ok(title, cfg):
    """
    Sprawdza, czy tytuł przechodzi filtr słów kluczowych.
    tryby: off (wyłączony), include (musi zawierać), exclude (nie może zawierać).
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
    """Buduje sformatowaną wiadomość HTML dla Telegrama."""
    title = html.escape(offer.get("title") or "Brak tytułu")
    price = html.escape(offer.get("price") or "Nie podano")
    location = html.escape(offer.get("location") or "Nie podano")
    offer_url = html.escape(offer.get("url") or "")
    return (
        "<b>Nowe ogłoszenie na OLX!</b>\n\n"
        f"<b>Tytuł:</b> {title}\n"
        f"<b>Cena:</b> {price}\n"
        f"<b>Lokalizacja:</b> {location}\n\n"
        f'<a href="{offer_url}">Zobacz ogłoszenie</a>'
    )


def build_offer_keyboard(offer):
    """Buduje przycisk prowadzący bezpośrednio do oferty."""
    return {
        "inline_keyboard": [
            [{"text": "Zobacz ofertę", "url": offer.get("url") or ""}]
        ]
    }


def send_telegram_message(cfg, text, reply_markup=None):
    """Wysyła wiadomość przez API Telegrama."""
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
        raise RuntimeError(data.get("description") or "Nieznany błąd Telegram API.")
    return data


def send_offer_notification(cfg, offer):
    """Wysyła powiadomienie o nowej ofercie."""
    send_telegram_message(
        cfg,
        build_offer_message(offer),
        reply_markup=build_offer_keyboard(offer),
    )


# ---------------------------------------------------------------------------
# Skaner (wątek w tle)
# ---------------------------------------------------------------------------


class ScannerThread(threading.Thread):
    """Wątek cyklicznie skanujący skonfigurowane adresy OLX."""

    def __init__(self):
        super().__init__(daemon=True, name="olx-scanner")

    def run(self):
        SCANNER_ALIVE.set()
        log.info("Skaner uruchomiony.")
        try:
            while not STOP_EVENT.is_set():
                cfg = load_config()
                if not cfg.get("urls"):
                    log.info("Brak adresów URL w konfiguracji. Oczekiwanie 60 s...")
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
                    log.exception("Nieoczekiwany błąd podczas skanowania.")
                self._sleep_with_abort(cfg)
        finally:
            SCANNER_ALIVE.clear()
            log.info("Skaner zatrzymany.")

    def _sleep_with_abort(self, cfg):
        """Uśpia wątek na losowy czas, reagując na zmianę konfiguracji."""
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
        """Wykonuje pojedynczy pełny skan wszystkich adresów URL."""
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
                    log.warning("Nie udało się pobrać %s (%s)", page_url, message)
                    break

                if looks_like_block(page_html):
                    with STATUS_LOCK:
                        STATUS["last_error"] = "OLX zablokował zapytanie (Cloudflare/captcha)."
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
                        log.info("Wysłano powiadomienie: %s", offer["title"][:80])
                    except Exception as exc:
                        message = describe_telegram_error(exc)
                        with STATUS_LOCK:
                            STATUS["last_error"] = message
                        log.error("Błąd wysyłania do Telegrama: %s", message)

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
            "Skan zakończony: sprawdzono %d ofert, nowych powiadomień: %d (czas: %.1f s)",
            checked,
            new_notifications,
            elapsed,
        )


# ---------------------------------------------------------------------------
# Panel WWW (Flask) - ciemny motyw w trybie Operate
# ---------------------------------------------------------------------------

app = Flask(__name__)

PANEL_HTML = """<!doctype html>
<html lang="pl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<title>OLX Monitor Bot</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Crect width='24' height='24' rx='6' fill='%2310b981'/%3E%3Ccircle cx='11' cy='11' r='5.5' fill='none' stroke='%23052e2b' stroke-width='2'/%3E%3Cpath d='M11 11 15.5 6.5' stroke='%23052e2b' stroke-width='2' stroke-linecap='round'/%3E%3C/svg%3E">
<style>
  :root {
    --bg: #0f172a;
    --surface: #1e293b;
    --field: #0b1220;
    --border: #334155;
    --text: #e2e8f0;
    --muted: #94a3b8;
    --accent: #10b981;
    --accent-strong: #34d399;
    --accent-ink: #052e26;
    --warn: #fbbf24;
    --err: #fb7185;
    --focus: #38bdf8;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  html { background-color: var(--bg); }
  body {
    min-height: 100vh;
    background-color: var(--bg);
    color: var(--text);
    font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
    font-size: 15px;
    line-height: 1.5;
  }
  ::selection { background: rgba(16,185,129,.30); color: #f8fafc; }
  .page { max-width: 1200px; margin: 0 auto; padding: 26px 20px 44px; }

  /* Nagłówek */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px; flex-wrap: wrap; margin-bottom: 22px;
  }
  .brand { display: flex; align-items: center; gap: 13px; }
  .logo {
    width: 42px; height: 42px; border-radius: 11px; display: grid; place-items: center;
    background: rgba(16,185,129,.14); border: 1px solid rgba(16,185,129,.35);
  }
  .logo svg { width: 22px; height: 22px; }
  .brand h1 { font-size: 19px; margin: 0; letter-spacing: .1px; }
  .brand p { margin: 3px 0 0; font-size: 12.5px; color: var(--muted); }

  .pill {
    display: inline-flex; align-items: center; gap: 9px; padding: 8px 14px;
    border-radius: 999px; font-size: 13px; font-weight: 600;
    border: 1px solid var(--border); background: var(--surface); color: var(--muted);
  }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: currentColor; }
  .pill.ok { color: #6ee7b7; border-color: rgba(16,185,129,.5); }
  .pill.busy { color: var(--warn); border-color: rgba(251,191,36,.5); }
  .pill.busy .dot { animation: pulse 1.2s ease-in-out infinite; }
  .pill.err { color: var(--err); border-color: rgba(251,113,133,.5); }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .3; } }

  /* Układ */
  .grid {
    display: grid; gap: 20px; align-items: start;
    grid-template-columns: minmax(0, 1.1fr) minmax(0, .9fr);
  }
  @media (max-width: 960px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: 0 1px 2px rgba(0,0,0,.35), 0 18px 40px -28px rgba(0,0,0,.85);
  }
  .card + .card { margin-top: 20px; }
  .card-head {
    padding: 15px 20px; border-bottom: 1px solid var(--border);
    display: flex; align-items: baseline; justify-content: space-between; gap: 10px;
  }
  .card-head h2 { margin: 0; font-size: 15px; font-weight: 650; color: #e8edf5; }
  .card-head .meta { font-size: 12px; color: var(--muted); }
  .card-body { padding: 20px; }

  /* Formularz */
  .group { margin-top: 20px; padding-top: 20px; border-top: 1px solid var(--border); }
  .group:first-child { margin-top: 0; padding-top: 0; border-top: none; }
  .group-title { margin: 0 0 12px; font-size: 13.5px; font-weight: 650; color: #cbd5e1; }
  .field { margin-bottom: 15px; }
  .field:last-child { margin-bottom: 0; }
  .label {
    display: block; font-size: 13px; font-weight: 600;
    color: var(--muted); margin-bottom: 7px;
  }
  .input, .select, .textarea {
    width: 100%; padding: 10px 12px; border-radius: 10px;
    background: var(--field); color: var(--text);
    border: 1px solid var(--border);
    font-size: 14px; font-family: inherit; outline: none;
    caret-color: var(--accent);
    transition: border-color .15s, box-shadow .15s;
  }
  .textarea { resize: vertical; min-height: 132px; line-height: 1.5; }
  .input::placeholder, .textarea::placeholder { color: var(--muted); opacity: 1; }
  .input:focus, .select:focus, .textarea:focus {
    border-color: var(--accent);
    box-shadow: 0 0 0 3px rgba(16,185,129,.22);
  }
  .mono { font-family: ui-monospace, "JetBrains Mono", Consolas, Menlo, monospace; font-variant-numeric: tabular-nums; }
  .hint { margin-top: 6px; font-size: 12.5px; color: var(--muted); }
  .row { display: grid; gap: 15px; }
  .row-2 { grid-template-columns: 1fr 1fr; }
  .row-3 { grid-template-columns: repeat(3, 1fr); }
  @media (max-width: 620px) { .row-2, .row-3 { grid-template-columns: 1fr; } }

  .check {
    display: flex; align-items: flex-start; gap: 10px; padding: 11px 12px;
    border: 1px solid var(--border); border-radius: 10px;
    background: var(--field); margin-bottom: 10px; cursor: pointer;
  }
  .check:last-of-type { margin-bottom: 0; }
  .check input { width: 17px; height: 17px; margin-top: 1px; accent-color: var(--accent); cursor: pointer; }
  .check span { font-size: 13.5px; color: #cbd5e1; }

  /* Przyciski */
  .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 18px; }
  .btn {
    border-radius: 10px; padding: 10px 16px;
    font-size: 14px; font-weight: 600; cursor: pointer;
    font-family: inherit; border: 1px solid transparent;
    transition: background-color .15s, border-color .15s, color .15s;
  }
  .btn-primary { background: var(--accent); color: var(--accent-ink); }
  .btn-primary:hover { background: var(--accent-strong); }
  .btn-secondary { background: transparent; border-color: var(--border); color: #cbd5e1; }
  .btn-secondary:hover { background: rgba(148,163,184,.10); border-color: #475569; }
  .btn:disabled { opacity: .55; cursor: not-allowed; }

  .feedback {
    margin-top: 14px; padding: 11px 13px; border-radius: 10px;
    font-size: 13.5px; display: none; border: 1px solid transparent;
  }
  .feedback.show { display: block; }
  .feedback.ok { background: rgba(16,185,129,.12); border-color: rgba(16,185,129,.4); color: #a7f3d0; }
  .feedback.err { background: rgba(251,113,133,.12); border-color: rgba(251,113,133,.45); color: #fecdd3; }
  .feedback.info { background: rgba(56,189,248,.10); border-color: rgba(56,189,248,.4); color: #bae6fd; }

  /* Status */
  .stats { display: flex; flex-direction: column; }
  .stat {
    display: flex; align-items: baseline; justify-content: space-between; gap: 14px;
    padding: 10px 0; border-bottom: 1px solid rgba(51,65,85,.6); font-size: 13.5px;
  }
  .stat:last-child { border-bottom: none; }
  .stat .k { color: var(--muted); }
  .stat .v { font-weight: 600; text-align: right; font-variant-numeric: tabular-nums; }
  .stat .v.quiet { color: var(--muted); font-weight: 500; }
  .stat .v.warn { color: var(--warn); font-weight: 500; }
  .badge {
    font-size: 12px; font-weight: 650; padding: 3px 9px;
    border-radius: 999px;
  }
  .badge.ok { background: rgba(16,185,129,.15); color: #a7f3d0; border: 1px solid rgba(16,185,129,.4); }
  .badge.off { background: rgba(148,163,184,.12); color: #cbd5e1; border: 1px solid rgba(148,163,184,.3); }

  /* Terminal logów */
  .terminal {
    background: var(--field); border: 1px solid var(--border); border-radius: 10px;
    padding: 13px; height: 330px; overflow-y: auto;
    font-family: ui-monospace, "JetBrains Mono", Consolas, Menlo, monospace;
    font-size: 12.5px; line-height: 1.7;
    font-variant-numeric: tabular-nums;
  }
  .terminal .ln { white-space: pre-wrap; word-break: break-word; color: #cbd5e1; }
  .terminal .ln.ok { color: #a7f3d0; }
  .terminal .ln.warn { color: var(--warn); }
  .terminal .ln.error { color: var(--err); }
  .terminal::-webkit-scrollbar { width: 9px; }
  .terminal::-webkit-scrollbar-thumb { background: #334155; border-radius: 6px; }
  .terminal::-webkit-scrollbar-track { background: transparent; }
  .live { display: inline-flex; align-items: center; gap: 7px; font-size: 12px; color: var(--muted); }
  .live .dot { width: 7px; height: 7px; border-radius: 50%; background: var(--accent); animation: pulse 1.6s infinite; }

  footer { margin-top: 22px; font-size: 12.5px; color: var(--muted); }
  footer code { color: #cbd5e1; font-family: ui-monospace, Consolas, Menlo, monospace; }

  /* Dostępność */
  .btn:focus-visible, .input:focus-visible, .select:focus-visible,
  .textarea:focus-visible, .check input:focus-visible {
    outline: 2px solid var(--focus); outline-offset: 2px;
  }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation: none !important; transition: none !important; }
  }
</style>
</head>
<body>
<div class="page">

  <div class="topbar">
    <div class="brand">
      <div class="logo" aria-hidden="true">
        <svg viewBox="0 0 24 24" fill="none" stroke="#34d399" stroke-width="2"
             stroke-linecap="round" stroke-linejoin="round">
          <circle cx="12" cy="12" r="8.5"></circle>
          <circle cx="12" cy="12" r="4"></circle>
          <path d="M12 12 18.5 5.5"></path>
        </svg>
      </div>
      <div>
        <h1>OLX Monitor Bot</h1>
        <p>Panel działa lokalnie (127.0.0.1) &middot; przeglądarkę możesz zamknąć — bot pracuje dalej</p>
      </div>
    </div>
    <div class="pill" id="status-pill" role="status" aria-live="polite">
      <span class="dot"></span><span id="status-text">Łączenie…</span>
    </div>
  </div>

  <div class="grid">

    <!-- LEWA KOLUMNA: KONFIGURACJA -->
    <section>
      <div class="card">
        <div class="card-head"><h2>Konfiguracja</h2></div>
        <div class="card-body">
          <form method="post" action="/save" id="config-form">

            <div class="group">
              <h3 class="group-title">Telegram</h3>
              <div class="row row-2">
                <div class="field">
                  <label class="label" for="f-token">Token bota</label>
                  <input id="f-token" class="input mono" type="text" name="telegram_bot_token"
                         value="{{ cfg.telegram_bot_token }}" placeholder="123456789:AAF…" autocomplete="off">
                </div>
                <div class="field">
                  <label class="label" for="f-chat">Chat ID</label>
                  <input id="f-chat" class="input mono" type="text" name="telegram_chat_id"
                         value="{{ cfg.telegram_chat_id }}" placeholder="123456789" inputmode="numeric" autocomplete="off">
                </div>
              </div>
            </div>

            <div class="group">
              <h3 class="group-title">Wyszukiwania OLX</h3>
              <div class="field">
                <label class="label" for="f-urls">Adresy URL (jeden na linię)</label>
                <textarea id="f-urls" class="textarea mono" name="urls"
                          placeholder="Wklej link skopiowany z paska adresu OLX…">{{ cfg.urls | join('\n') }}</textarea>
                <div class="hint">Skopiuj adres z OLX razem z filtrami (cena, promień, sortowanie od najnowszych).</div>
              </div>
              <div class="row row-2">
                <div class="field">
                  <label class="label" for="f-keywords">Słowa kluczowe</label>
                  <input id="f-keywords" class="input" type="text" name="keywords"
                         value="{{ cfg.keywords }}" placeholder="np. iphone, nieuszkodzony">
                  <div class="hint">Oddziel przecinkami. Puste pole wyłącza filtr.</div>
                </div>
                <div class="field">
                  <label class="label" for="f-mode">Tryb filtra</label>
                  <select id="f-mode" class="select" name="keyword_mode">
                    <option value="off" {% if cfg.keyword_mode == 'off' %}selected{% endif %}>Wyłączony</option>
                    <option value="include" {% if cfg.keyword_mode == 'include' %}selected{% endif %}>Zawiera</option>
                    <option value="exclude" {% if cfg.keyword_mode == 'exclude' %}selected{% endif %}>Nie zawiera</option>
                  </select>
                </div>
              </div>
            </div>

            <div class="group">
              <h3 class="group-title">Harmonogram</h3>
              <div class="row row-3">
                <div class="field">
                  <label class="label" for="f-min">Min. interwał (s)</label>
                  <input id="f-min" class="input" type="number" name="min_interval" min="30"
                         value="{{ cfg.min_interval }}">
                </div>
                <div class="field">
                  <label class="label" for="f-max">Maks. interwał (s)</label>
                  <input id="f-max" class="input" type="number" name="max_interval" min="30"
                         value="{{ cfg.max_interval }}">
                </div>
                <div class="field">
                  <label class="label" for="f-pages">Liczba stron</label>
                  <input id="f-pages" class="input" type="number" name="max_pages" min="1" max="10"
                         value="{{ cfg.max_pages }}">
                </div>
              </div>
              <div class="hint">Program losuje odstęp między skanami z tego przedziału. Minimum to 30 s.</div>
            </div>

            <div class="group">
              <h3 class="group-title">Opcje powiadomień</h3>
              <label class="check">
                <input type="checkbox" name="telegram_enabled" {% if cfg.telegram_enabled %}checked{% endif %}>
                <span>Wysyłaj powiadomienia Telegram</span>
              </label>
              <label class="check">
                <input type="checkbox" name="notify_first_scan" {% if cfg.notify_first_scan %}checked{% endif %}>
                <span>Powiadamiaj o wszystkich ofertach z pierwszego skanu</span>
              </label>
            </div>

            <div class="actions">
              <button type="submit" class="btn btn-primary">Zapisz konfigurację</button>
              <button type="button" class="btn btn-secondary" onclick="runAction('/test')">Test wiadomości</button>
              <button type="button" class="btn btn-secondary" onclick="runAction('/scan_now')">Skanuj teraz</button>
            </div>

            <div id="action-feedback" class="feedback" role="status" aria-live="polite"></div>
          </form>
        </div>
      </div>
    </section>

    <!-- PRAWA KOLUMNA: STATUS I LOGI -->
    <section>
      <div class="card">
        <div class="card-head"><h2>Status</h2></div>
        <div class="card-body" aria-label="Status monitorowania">
          <div class="stats">
            <div class="stat"><span class="k">Ostatni skan</span><span class="v quiet" id="st-last-scan">—</span></div>
            <div class="stat"><span class="k">Nowe oferty w ostatnim skanie</span><span class="v" id="st-last-found">—</span></div>
            <div class="stat"><span class="k">Następny skan za</span><span class="v mono" id="st-next">—</span></div>
            <div class="stat"><span class="k">Zapamiętane oferty</span><span class="v" id="st-seen">—</span></div>
            <div class="stat"><span class="k">Wysłane powiadomienia</span><span class="v" id="st-sent">—</span></div>
            <div class="stat"><span class="k">Liczba adresów URL</span><span class="v" id="st-urls">—</span></div>
            <div class="stat"><span class="k">Status Telegrama</span><span class="v"><span id="st-tg" class="badge off">—</span></span></div>
            <div class="stat"><span class="k">Czas działania</span><span class="v mono" id="st-uptime">—</span></div>
            <div class="stat"><span class="k">Ostatni błąd</span><span class="v quiet" id="st-error">—</span></div>
          </div>
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h2>Dziennik zdarzeń</h2>
          <span class="live"><span class="dot"></span>na żywo</span>
        </div>
        <div class="card-body">
          <div id="log-box" class="terminal" role="log" aria-live="polite" aria-label="Dziennik zdarzeń">Ładowanie logu…</div>
        </div>
      </div>
    </section>

  </div>

  <footer>
    Historia ofert: <code>seen_offers.json</code> &middot;
    Konfiguracja: <code>config.json</code> &middot;
    Log: <code>bot.log</code><br>
    Zatrzymanie bota: <code>stop.sh</code> (macOS/Linux) lub <code>stop.bat</code> (Windows).
  </footer>
</div>

<script>
  const $ = (id) => document.getElementById(id);
  const SAVE_STATE = {{ save_state | tojson }};
  let lastLogs = '';

  function fmtDuration(s) {
    if (s === null || s === undefined) return '—';
    s = Math.max(0, Math.floor(s));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    if (h > 0) return h + ' godz. ' + m + ' min';
    if (m > 0) return m + ' min ' + sec + ' s';
    return sec + ' s';
  }

  function setStatus(state, label) {
    const pill = $('status-pill');
    pill.className = 'pill' + (state ? ' ' + state : '');
    $('status-text').textContent = label;
  }

  function renderLogs(lines) {
    const box = $('log-box');
    const nearBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 48;
    const frag = document.createDocumentFragment();
    lines.forEach(function (line) {
      const el = document.createElement('div');
      el.className = 'ln';
      if (line.indexOf('| ERROR |') !== -1) el.classList.add('error');
      else if (line.indexOf('| WARNING |') !== -1) el.classList.add('warn');
      else if (line.indexOf('| INFO |') !== -1) el.classList.add('ok');
      el.textContent = line;
      frag.appendChild(el);
    });
    box.replaceChildren(frag);
    if (nearBottom) box.scrollTop = box.scrollHeight;
  }

  function feedback(message, kind) {
    const box = $('action-feedback');
    box.className = 'feedback show ' + kind;
    box.textContent = message;
  }

  async function refresh() {
    let d;
    try {
      const r = await fetch('/api/status');
      d = await r.json();
    } catch (e) {
      setStatus('err', 'Brak połączenia');
      return;
    }
    if (!d.scanner_running) setStatus('err', 'Zatrzymany');
    else if (d.scanning_now) setStatus('busy', 'Skanowanie…');
    else setStatus('ok', 'Oczekuje');

    $('st-last-scan').textContent = d.last_scan || 'jeszcze nie było';
    $('st-last-found').textContent = d.last_scan_found;
    $('st-next').textContent = fmtDuration(d.next_scan_in);
    $('st-seen').textContent = d.seen_count;
    $('st-sent').textContent = d.sent_total;
    $('st-urls').textContent = d.urls_count;
    $('st-uptime').textContent = fmtDuration(d.uptime);
    $('st-error').textContent = d.last_error || 'brak';

    const tg = $('st-tg');
    tg.textContent = d.telegram_configured ? 'Skonfigurowany' : 'Brak danych';
    tg.className = 'badge ' + (d.telegram_configured ? 'ok' : 'off');

    const text = d.logs.join('\n');
    if (text !== lastLogs) {
      lastLogs = text;
      renderLogs(d.logs);
    }
  }

  function formPayload() {
    const fd = new FormData();
    fd.append('telegram_bot_token', $('f-token').value);
    fd.append('telegram_chat_id', $('f-chat').value);
    fd.append('urls', $('f-urls').value);
    fd.append('keywords', $('f-keywords').value);
    fd.append('keyword_mode', $('f-mode').value);
    fd.append('min_interval', $('f-min').value);
    fd.append('max_interval', $('f-max').value);
    fd.append('max_pages', $('f-pages').value);
    if (document.querySelector('input[name="telegram_enabled"]').checked) fd.append('telegram_enabled', 'on');
    if (document.querySelector('input[name="notify_first_scan"]').checked) fd.append('notify_first_scan', 'on');
    fd.append('ajax', '1');
    return fd;
  }

  async function runAction(url) {
    feedback('Zapisywanie zmian…', 'info');
    let saved;
    try {
      const r = await fetch('/save', { method: 'POST', body: formPayload() });
      saved = await r.json();
    } catch (e) {
      feedback('Nie udało się połączyć z serwerem.', 'err');
      return;
    }
    if (!saved.ok) {
      feedback(saved.message || 'Nie udało się zapisać konfiguracji.', 'err');
      return;
    }
    feedback('Wykonywanie…', 'info');
    try {
      const r = await fetch(url, { method: 'POST' });
      const d = await r.json();
      feedback(d.message, d.ok ? 'ok' : 'err');
    } catch (e) {
      feedback('Nie udało się połączyć z serwerem.', 'err');
    }
  }

  if (SAVE_STATE === '1') feedback('Konfiguracja została zapisana.', 'ok');
  else if (SAVE_STATE === '0') feedback('Nie udało się zapisać konfiguracji.', 'err');
  if (SAVE_STATE !== null) history.replaceState(null, '', '/');

  setInterval(refresh, 5000);
  refresh();
</script>
</body>
</html>
"""


def to_int(value, default, lo, hi):
    """Bezpieczna konwersja na int z obcięciem do przedziału [lo, hi]."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, number))


@app.get("/")
def index():
    """Główna strona panelu."""
    cfg = load_config()
    save_state = request.args.get("saved")
    return render_template_string(PANEL_HTML, cfg=cfg, save_state=save_state)


@app.post("/save")
def save():
    """Zapisuje konfigurację przesłaną z formularza."""
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

    ok = save_config(cfg)
    if ok:
        log.info(
            "Konfiguracja zapisana (URL: %d, interwał: %d-%d s, strony: %d).",
            len(cfg["urls"]),
            cfg["min_interval"],
            cfg["max_interval"],
            cfg["max_pages"],
        )
    else:
        log.error("Nie udało się zapisać konfiguracji.")

    if request.form.get("ajax") == "1":
        return jsonify(
            ok=ok,
            message="Konfiguracja została zapisana." if ok else "Nie udało się zapisać konfiguracji.",
        )
    return redirect("/?saved=1" if ok else "/?saved=0")


@app.post("/test")
def test_telegram():
    """Wysyła testową wiadomość do Telegrama."""
    cfg = load_config()
    if not (cfg.get("telegram_bot_token") and cfg.get("telegram_chat_id")):
        return jsonify(
            ok=False,
            message="Uzupełnij Token bota i Chat ID, zapisz konfigurację "
                    "i wyślij /start do swojego bota w Telegramie.",
        )
    try:
        send_telegram_message(
            cfg,
            "<b>Test powiadomień OLX Monitor Bot</b>\n\n"
            "Połączenie działa poprawnie.",
        )
        log.info("Testowa wiadomość Telegram wysłana pomyślnie.")
        return jsonify(ok=True, message="Wiadomość testowa wysłana. Sprawdź Telegram.")
    except Exception as exc:
        message = describe_telegram_error(exc)
        log.error("Test Telegram nieudany: %s", message)
        return jsonify(ok=False, message=message)


@app.post("/scan_now")
def scan_now():
    """Wymusza natychmiastowy skan."""
    SCAN_NOW_EVENT.set()
    return jsonify(ok=True, message="Skan zostanie uruchomiony za chwilę.")


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
    """Pusty favicon (unikanie wpisów 404 w logu)."""
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
    """Punkt wejścia aplikacji."""
    setup_logging()
    log.info("Uruchamianie OLX Monitor Bot...")
    if HAS_CURL_CFFI:
        log.info("Klient HTTP: curl_cffi (imitacja przeglądarki %s).", BROWSER_IMITATE)
    else:
        log.warning(
            "Klient HTTP: requests (curl_cffi niedostępne - tryb zgodności, "
            "np. Android/Termux). OLX może częściej stosować blokady."
        )

    scanner = ScannerThread()
    scanner.start()

    port = find_free_port()
    log.info("Panel administracyjny: http://127.0.0.1:%d", port)
    banner = [
        "============================================================",
        "  OLX Monitor Bot",
        f"  Panel administracyjny:  http://127.0.0.1:{port}",
        "  Aby zatrzymać aplikację naciśnij Ctrl+C",
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
