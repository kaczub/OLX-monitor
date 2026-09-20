@echo off
chcp 65001 >nul
title OLX Monitor
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo [BLAD] Nie znaleziono Pythona w PATH.
    echo Zainstaluj Python 3.10 lub nowszy ze strony https://www.python.org/downloads/
    echo Podczas instalacji zaznacz opcje "Add Python to PATH".
    pause
    exit /b 1
)

if not exist "venv\Scripts\python.exe" (
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

echo.
echo ============================================================
echo  OLX Monitor - uruchamianie...
echo  Panel: http://127.0.0.1:5000
echo  Aby zatrzymac: zamknij to okno lub nacisnij Ctrl+C
echo ============================================================
echo.
python app.py
pause
