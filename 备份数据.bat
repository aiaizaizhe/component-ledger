@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul && (python backup.py) || (py -3 backup.py)
echo.
pause >nul
