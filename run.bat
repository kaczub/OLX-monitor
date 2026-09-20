@echo off
setlocal enableextensions
cd /d "%~dp0"
title OLX Monitor

set "LOG=%~dp0run-log.txt"
echo ==== OLX Monitor - uruchomienie %DATE% %TIME% ==== > "%LOG%"

REM --- Znajdz dzialajacego Pythona (najpierw launcher "py", potem python bez zaslepki Store) ---
set "PY="
py -3 --version >nul 2>nul && set "PY=py -3"
if not defined PY (
    for /f "delims=" %%i in ('where python 2^>nul') do (
        echo %%i | findstr /i "WindowsApps" >nul
        if errorlevel 1 set "PY=python"
    )
)

if not defined PY (
    echo [BLAD] Nie znaleziono dzialajacego Pythona.
    echo.
    echo Zainstaluj Python 3.10 lub nowszy ze strony https://www.python.org/downloads/
    echo Podczas instalacji ZAZNACZ opcje "Add python.exe to PATH".
    echo.
    echo Szczegoly zapisano w: %LOG%
    pause
    exit /b 1
)

%PY% --version >>"%LOG%" 2>&1

if not exist "venv\Scripts\python.exe" (
    if exist "venv" rmdir /s /q venv
    echo Tworzenie srodowiska wirtualnego (pierwsze uruchomienie)...
    %PY% -m venv venv >>"%LOG%" 2>&1
    if errorlevel 1 (
        echo [BLAD] Nie udalo sie utworzyc srodowiska wirtualnego.
        echo Szczegoly zapisano w: %LOG%
        pause
        exit /b 1
    )
)

call "venv\Scripts\activate.bat"

echo Instalowanie / aktualizowanie zaleznosci...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check >>"%LOG%" 2>&1
if errorlevel 1 (
    echo [BLAD] Nie udalo sie zainstalowac zaleznosci. Sprawdz polaczenie z internetem.
    echo Szczegoly zapisano w: %LOG%
    pause
    exit /b 1
)

echo.
echo ============================================================
echo  OLX Monitor - uruchamianie...
echo  Adres panelu wyswietli sie ponizej (domyslnie http://127.0.0.1:5000)
echo  Aby zatrzymac: zamknij to okno lub nacisnij Ctrl+C
echo ============================================================
echo.
python app.py
echo.
echo Program zakonczyl dzialanie. Ewentualne bledy znajdziesz w: %LOG%
pause
