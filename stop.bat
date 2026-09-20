@echo off
title OLX Monitor - zatrzymywanie
cd /d "%~dp0"

powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' or Name='python.exe'\" | Where-Object { $_.CommandLine -like '*app.py*' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host 'Bot zatrzymany.' } else { Write-Host 'Bot nie dziala.' }"

timeout /t 4 >nul
