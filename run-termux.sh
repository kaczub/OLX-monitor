#!/usr/bin/env bash
# Skrypt startowy dla Android / Termux.
# Instaluje zaleznosci (bez curl_cffi) i uruchamia aplikacje.
set -e
cd "$(dirname "$0")"

if ! command -v python >/dev/null 2>&1; then
  echo "[BLAD] Nie znaleziono Pythona."
  echo "W Termuxie zainstaluj go komenda: pkg install python -y"
  exit 1
fi

echo "Instalowanie / aktualizowanie zależności (tryb Termux)..."
pip install -r requirements-termux.txt --quiet --disable-pip-version-check

# Zapobiega usypianiu Termuxa przez system Android (jesli narzedzie dostepne).
if command -v termux-wake-lock >/dev/null 2>&1; then
  termux-wake-lock
  echo "Wake-lock włączony (Android nie uśpi Termuxa)."
fi

echo ""
echo "============================================================"
echo " OLX Monitor - uruchamianie..."
echo " Panel (na tym telefonie): adres wyświetli się poniżej"
echo " Aby zatrzymać: naciśnij Ctrl+C"
echo "============================================================"
echo ""
python app.py
