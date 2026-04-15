@echo off

python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python is not installed.
    echo Please download and install Python from https://python.org/
    echo After installation, restart this script.
    pause
    exit /b 1
)

REM 检查 Python 版本是否 >= 3.6
for /f "tokens=2" %%v in ('python --version 2^>^&1') do set PY_VER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PY_VER%") do (
    set PY_MAJOR=%%a
    set PY_MINOR=%%b
)
if %PY_MAJOR% LSS 3 (
    echo [ERROR] Python 3.6 or higher is required. Current version: %PY_VER%
    pause
    exit /b 1
)
if %PY_MAJOR% EQU 3 if %PY_MINOR% LSS 6 (
    echo [ERROR] Python 3.6 or higher is required. Current version: %PY_VER%
    pause
    exit /b 1
)

echo Python version %PY_VER% is OK.

echo Checking requirements...
if not exist "venv" (
    echo Load virtual environment...
    python -m venv venv
    echo Setup requirements...
    call venv\Scripts\activate.bat
    pip install -r requirements.txt
) else (
    call venv\Scripts\activate.bat
)

echo Starting Webs...
python main.py
pause