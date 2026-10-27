@echo off
chcp 65001 >nul
title 更换数据位置
cd /d "%~dp0"

echo.
echo   当前数据放在哪里，可以在这里改。
echo   建议用一个单独的文件夹，例如： D:\元件库数据
echo   直接回车 = 不改。
echo.
set /p DATADIR=请输入数据文件夹路径：

if "%DATADIR%"=="" (
  echo 没有修改。
  pause
  exit /b 0
)

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo 没有找到 Python。
  pause
  exit /b 1
)

%PY% set_data_dir.py "%DATADIR%"
echo.
pause >nul
