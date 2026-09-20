#!/usr/bin/env bash
# Skrypt startowy dla Linux / macOS.
# Tworzy srodowisko wirtualne (przy pierwszym uruchomieniu),
# instaluje zaleznosci i uruchamia aplikacje.
set -e
cd "$(dirname "$0")"

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

echo ""
echo "============================================================"
echo " OLX Monitor Bot - uruchamianie..."
echo " Panel: http://127.0.0.1:5000"
echo " Aby zatrzymać: naciśnij Ctrl+C"
echo "============================================================"
echo ""
python app.py
