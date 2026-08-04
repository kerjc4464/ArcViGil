@echo off
chcp 65001 >nul
title ArcViGil Backend
echo =======================================
echo     ArcViGil Backend Starting...
echo =======================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.8+ and add it to PATH.
    echo Download: https://www.python.org/downloads/
    pause
    exit /b
)

echo [1/2] Installing dependencies...
pip install -r requirements.txt -q
if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install dependencies. Please check your network and try again.
    pause
    exit /b
)
echo Dependencies ready.
echo.

echo [BUILD] Starting backend server on port 9000...
echo Keep this window open. ArcViGil is watching...
echo.
python server.py

pause
