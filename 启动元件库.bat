@echo off
setlocal
cd /d "%~dp0"
title Component Ledger

rem Keep this file ASCII-only: cmd.exe misparses UTF-8 batch files,
rem which truncates lines and breaks the launcher. Chinese output comes
rem from server.py (it switches the console to UTF-8 itself).

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo.
  echo   Python not found.
  echo   Please install Python 3.9 or newer, and tick "Add Python to PATH".
  echo.
  pause
  exit /b 1
)

%PY% server.py --open
set "RC=%errorlevel%"

rem exit code 3 = an instance is already running, browser was just opened
if "%RC%"=="3" exit /b 0

echo.
echo   Stopped. Your data is still saved in the data folder.
echo   Press any key to close this window.
pause >nul
