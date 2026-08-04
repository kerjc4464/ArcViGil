@echo off
chcp 65001 >nul
title ArcViGil Backend
echo =======================================
echo     ArcViGil Backend Starting...
echo =======================================
echo.

python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python and add it to PATH.
    pause
    exit /b
)

echo [1/2] Installing dependencies...
pip install -r requirements.txt -q
echo.

echo [2/2] Starting backend server on port 9000...
echo Keep this window open. ArcViGil is watching...
echo.
python server.py

pause
