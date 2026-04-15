#!/bin/bash

# 检查 Python 是否安装
if ! command -v python3 &> /dev/null; then
    echo "[ERROR] Python3 is not installed."
    echo "Please install Python 3.6 or higher from https://python.org/"
    exit 1
fi

# 获取 Python 版本
PY_VER=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)

# 检查版本是否 >= 3.6
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 6 ]; }; then
    echo "[ERROR] Python 3.6 or higher is required. Current version: $PY_VER"
    exit 1
fi

echo "Python version $PY_VER is OK."

echo "Checking requirements..."
if [ ! -d "venv" ]; then
    echo "Load virtual environment..."
    python3 -m venv venv
    echo "Setup requirements..."
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

echo "Starting Webs..."
python3 main.py