@echo off
chcp 65001 >nul
title OLX Monitor - uruchamianie w tle
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [BLAD] Nie znaleziono Pythona w PATH.
    echo Zainstaluj Python 3.10 lub nowszy ze strony https://www.python.org/downloads/
    echo Podczas instalacji zaznacz opcje "Add Python to PATH".
    pause
    exit /b 1
)

powershell -NoProfile -Command "if (Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe'\" | Where-Object { $_.CommandLine -like '*app.py*' }) { exit 1 }"
if errorlevel 1 (
    echo Bot juz dziala w tle.
    echo Zatrzymanie: stop.bat
    timeout /t 4 >nul
    exit /b 0
)

if not exist "venv\Scripts\python.exe" (
    if exist "venv" rmdir /s /q venv
    echo Tworzenie srodowiska wirtualnego (pierwsze uruchomienie)...
    python -m venv venv
    if errorlevel 1 (
        echo [BLAD] Nie udalo sie utworzyc srodowiska wirtualnego.
        pause
        exit /b 1
    )
)

call "venv\Scripts\activate.bat"

echo Instalowanie / aktualizowanie zaleznosci...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
if errorlevel 1 (
    echo [BLAD] Nie udalo sie zainstalowac zaleznosci. Sprawdz polaczenie z internetem.
    pause
    exit /b 1
)

echo Uruchamianie bota w tle (bez okna konsoli)...
powershell -NoProfile -Command "Start-Process -WindowStyle Hidden -FilePath '.\venv\Scripts\pythonw.exe' -ArgumentList 'app.py' -WorkingDirectory '%CD%'"

timeout /t 3 >nul
set "PANEL_URL="
for /f "delims=" %%u in ('powershell -NoProfile -Command "$m = Select-String -Path bot.log -Pattern ''http://127.0.0.1:[0-9]+'' | Select-Object -Last 1; if ($m) { [regex]::Match($m.Line, ''http://127.0.0.1:[0-9]+'').Value }"') do set "PANEL_URL=%%u"
if not defined PANEL_URL set "PANEL_URL=http://127.0.0.1:5000"

echo.
echo Bot uruchomiony w tle.
echo Panel: %PANEL_URL%
echo Mozesz zamknac to okno oraz przegladarke.
echo Zatrzymanie: stop.bat
start "" "%PANEL_URL%"
timeout /t 5 >nul
