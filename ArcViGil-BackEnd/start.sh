#!/bin/bash
# ArcViGil Backend Launcher (macOS / Linux)
# 用法: chmod +x start.sh && ./start.sh
# 需要: Python 3.10+ (请去 python.org 下载 3.13)

cd "$(dirname "$0")"

echo "======================================="
echo "    ArcViGil Backend Starting..."
echo "======================================="
echo ""

# 1. 找 python3
if ! command -v python3 >/dev/null 2>&1; then
    echo "[ERROR] python3 not found."
    echo "请去 https://www.python.org/downloads/ 安装 Python 3.13，"
    echo "然后重新运行 ./start.sh"
    exit 1
fi

echo "[OK] Python found: $(command -v python3)"
python3 --version
echo ""

# 2. 检查版本 >= 3.10 (新 FastAPI 硬要求)
if ! python3 -c "import sys; exit(0 if sys.version_info>=(3,10) else 1)"; then
    echo "[WARN] Python 版本低于 3.10，FastAPI 新版可能装不上。"
    echo "建议去 python.org 装 3.13 后再试。"
    echo ""
fi

# 3. venv (隔离 Homebrew PEP668 / 系统污染问题)
if [ ! -d ".venv" ]; then
    echo "[1/2] Creating venv (.venv)..."
    python3 -m venv .venv
    echo ""
fi

# shellcheck disable=SC1091
source .venv/bin/activate

echo "[1/2] Installing dependencies..."
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt -q
    if [ $? -ne 0 ]; then
        echo "[WARN] Quiet install failed, retrying verbose..."
        pip install -r requirements.txt
        if [ $? -ne 0 ]; then
            echo ""
            echo "[ERROR] Failed to install dependencies."
            echo "可试国内源: pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
            exit 1
        fi
    fi
    echo "Dependencies ready."
    echo ""
else
    echo "[WARN] requirements.txt not found, skipping install."
fi

# 4. 启动
echo "[2/2] Starting backend server on port 9000..."
echo "Keep this terminal open. ArcViGil is watching..."
echo ""

if [ ! -f "server.py" ]; then
    echo "[ERROR] server.py not found."
    echo "请在 ArcViGil-BackEnd 目录下运行 ./start.sh"
    exit 1
fi

python server.py
