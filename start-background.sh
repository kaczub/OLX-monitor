#!/usr/bin/env bash
# Uruchamia bota w tle (macOS / Linux).
# Po uruchomieniu mozesz zamknac terminal i przegladarke - bot dziala dalej.
set -e
cd "$(dirname "$0")"

if [ -f "bot.pid" ] && kill -0 "$(cat bot.pid)" 2>/dev/null; then
  echo "Bot juz dziala (PID $(cat bot.pid))."
  echo "Zatrzymanie: ./stop.sh"
  exit 0
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "[BLAD] Nie znaleziono polecenia python3."
  echo "Zainstaluj Python 3.10 lub nowszy: https://www.python.org/downloads/"
  exit 1
fi

if [ ! -f "venv/bin/python" ]; then
  echo "Tworzenie środowiska wirtualnego (pierwsze uruchomienie)..."
  python3 -m venv venv
fi

# shellcheck disable=SC1091
source venv/bin/activate
echo "Instalowanie / aktualizowanie zależności..."
pip install -r requirements.txt --quiet --disable-pip-version-check

nohup python app.py </dev/null > /dev/null 2>&1 &
echo $! > bot.pid
disown 2>/dev/null || true
sleep 3

if kill -0 "$(cat bot.pid)" 2>/dev/null; then
  PORT="$(grep -o 'http://127.0.0.1:[0-9]*' bot.log 2>/dev/null | tail -1 | grep -o '[0-9]*$')"
  [ -z "$PORT" ] && PORT=5000
  URL="http://127.0.0.1:$PORT"
  echo ""
  echo "Bot uruchomiony w tle (PID $(cat bot.pid))."
  echo "Panel: $URL"
  echo "Możesz teraz zamknąć terminal i przeglądarkę."
  echo "Zatrzymanie: ./stop.sh"
  # Otwórz panel w domyślnej przeglądarce (jeśli system na to pozwala).
  if command -v open >/dev/null 2>&1; then
    open "$URL" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 || true
  fi
else
  echo "[BLAD] Bot nie wystartowal. Sprawdz plik bot.log."
  rm -f bot.pid
  exit 1
fi
