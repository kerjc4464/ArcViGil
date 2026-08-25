@echo off
chcp 65001 >nul
setlocal EnableExtensions
title ArcViGil Backend
echo =======================================
echo     ArcViGil Backend Starting...
echo =======================================
echo.

REM ============================================================
REM  1. Locate Python interpreter (compatible with various installs)
REM  Priority: python -> py (Windows launcher) -> python3
REM ============================================================
set "PY_CMD="

python --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python"
    goto :FoundPython
)

py --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=py"
    goto :FoundPython
)

python3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=python3"
    goto :FoundPython
)

REM --- No Python found ---
echo [ERROR] Python not found.
echo.
echo  Please install Python 3.8+ and check "Add to PATH" during installation.
echo  Download: https://www.python.org/downloads/
echo.
echo  If Python is already installed, try manually in this folder:
echo    python --version
echo    py --version
echo    python3 --version
echo.
echo  Common fix: Reinstall Python from python.org and MUST check:
echo    [x] Add python.exe to PATH  (bottom of installer)
echo.
pause
exit /b 1

:FoundPython
echo [OK] Python found: %PY_CMD%
%PY_CMD% --version
echo.

REM --- Check Python version >= 3.8 ---
%PY_CMD% -c "import sys; exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>&1
if %errorlevel% neq 0 (
    echo [WARN] Python version is below 3.8, may cause dependency issues.
    echo        Recommended: Python 3.8 - 3.12 from https://www.python.org/downloads/
    echo.
)

REM ============================================================
REM  2. Locate pip (most reliable is "python -m pip")
REM  Priority: %PY_CMD% -m pip -> pip -> pip3 -> py -m pip -> ensurepip
REM ============================================================
set "PIP_MODE=none"
set "PIP_DESC="

%PY_CMD% -m pip --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PIP_MODE=py_m_pip"
    set "PIP_DESC=%PY_CMD% -m pip"
    goto :FoundPip
)

pip --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PIP_MODE=pip"
    set "PIP_DESC=pip"
    goto :FoundPip
)

pip3 --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PIP_MODE=pip3"
    set "PIP_DESC=pip3"
    goto :FoundPip
)

REM Last resort: try py launcher even if PY_CMD was python
py -m pip --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PY_CMD=py"
    set "PIP_MODE=py_m_pip"
    set "PIP_DESC=py -m pip"
    goto :FoundPip
)

REM Try to bootstrap pip via ensurepip (fixes stripped installs / Store Python)
echo [WARN] pip not found via PATH, trying to bootstrap with ensurepip...
%PY_CMD% -m ensurepip --upgrade >nul 2>&1
%PY_CMD% -m pip --version >nul 2>&1
if %errorlevel% equ 0 (
    set "PIP_MODE=py_m_pip"
    set "PIP_DESC=%PY_CMD% -m pip (via ensurepip)"
    goto :FoundPip
)

REM --- No pip found ---
:NoPip
if "%PIP_MODE%"=="none" (
    echo [ERROR] pip not found.
    echo.
    echo  Python is available (%PY_CMD%) but pip is not in PATH.
    echo  This happens when "Add to PATH" was unchecked or Python is from Microsoft Store.
    echo.
    echo  Please try manually in this folder:
    echo    %PY_CMD% -m pip --version
    echo    %PY_CMD% -m pip install -r requirements.txt
    echo.
    echo  If that works, you can start the backend manually:
    echo    %PY_CMD% server.py
    echo.
    echo  Permanent fixes (pick one):
    echo    1) Reinstall Python from https://www.python.org/downloads/
    echo       and CHECK "Add python.exe to PATH" + "pip"
    echo    2) Add Scripts folder to PATH, e.g.:
    echo       C:\Users\%%USERNAME%%\AppData\Local\Programs\Python\Python312\Scripts\
    echo    3) Always use "%PY_CMD% -m pip" instead of "pip"
    echo.
    pause
    exit /b 1
)

:FoundPip
echo [OK] pip found: %PIP_DESC%
if "%PIP_MODE%"=="py_m_pip" (
    %PY_CMD% -m pip --version
) else if "%PIP_MODE%"=="pip" (
    pip --version
) else (
    pip3 --version
)
echo.

REM ============================================================
REM  3. Install dependencies
REM ============================================================
if not exist "requirements.txt" (
    echo [WARN] requirements.txt not found, skipping install.
    goto :StartServer
)

echo [1/2] Installing dependencies via %PIP_DESC% ...
echo.

if "%PIP_MODE%"=="py_m_pip" (
    %PY_CMD% -m pip install -r requirements.txt -q
) else if "%PIP_MODE%"=="pip" (
    pip install -r requirements.txt -q
) else (
    pip3 install -r requirements.txt -q
)

if %errorlevel% equ 0 (
    echo Dependencies ready.
    echo.
    goto :StartServer
)

REM --- Install failed: show verbose output for diagnosis ---
echo [WARN] Quiet install failed, retrying with verbose output...
echo.
if "%PIP_MODE%"=="py_m_pip" (
    %PY_CMD% -m pip install -r requirements.txt
) else if "%PIP_MODE%"=="pip" (
    pip install -r requirements.txt
) else (
    pip3 install -r requirements.txt
)

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Failed to install dependencies.
    echo.
    echo  Possible causes:
    echo    - No network / proxy issue
    echo    - pip source blocked (try: %PIP_DESC% install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple)
    echo    - Permission denied (try running as Administrator)
    echo.
    echo  You can also try manually:
    echo    %PY_CMD% -m pip install -r requirements.txt
    echo    %PY_CMD% -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo.
    pause
    exit /b 1
)

echo Dependencies ready.
echo.

REM ============================================================
REM  4. Start backend server
REM ============================================================
:StartServer
echo [2/2] Starting backend server on port 9000...
echo Keep this window open. ArcViGil is watching...
echo.

if not exist "server.py" (
    echo [ERROR] server.py not found in current directory.
    echo Current dir: %CD%
    echo Please run start.bat inside ArcViGil-BackEnd folder.
    pause
    exit /b 1
)

%PY_CMD% server.py

if %errorlevel% neq 0 (
    echo.
    echo [ERROR] Backend exited with code %errorlevel%.
    echo Check the error above. Common fixes:
    echo   - Port 9000 occupied: netstat -ano ^| findstr :9000
    echo   - Missing dependency: %PIP_DESC% install -r requirements.txt
    echo.
)

pause
endlocal
