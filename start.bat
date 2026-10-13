@echo off
cd /d "%~dp0"
title TikTok Username Checker

where py >nul 2>nul
if %errorlevel%==0 (
    set PY=py
) else (
    where python >nul 2>nul
    if %errorlevel%==0 (
        set PY=python
    ) else (
        echo Python was not found.
        echo Please install Python from https://www.python.org/downloads/
        echo IMPORTANT: tick "Add python.exe to PATH" during installation!
        pause
        exit /b
    )
)

%PY% -c "import requests, colorama" >nul 2>nul
if not %errorlevel%==0 (
    echo Installing required packages, one moment...
    %PY% -m pip install requests colorama
)

cls
%PY% tiktok_checker.py
echo.
pause
