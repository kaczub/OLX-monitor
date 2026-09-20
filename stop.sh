#!/usr/bin/env bash
# Zatrzymuje bota uruchomionego w tle (macOS / Linux).
cd "$(dirname "$0")" || exit 1

if [ ! -f "bot.pid" ]; then
  echo "Brak pliku bot.pid - bot prawdopodobnie nie dziala."
  exit 1
fi

PID="$(cat bot.pid)"
if kill "$PID" 2>/dev/null; then
  echo "Bot zatrzymany (PID $PID)."
else
  echo "Proces (PID $PID) juz nie dziala."
fi
rm -f bot.pid
