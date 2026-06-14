@echo off
echo === Nodus Setup ===
echo.

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python not found. Install Python 3.11+ first.
    exit /b 1
)

REM Create venv
if not exist venv (
    python -m venv venv
    echo Venv created.
)

REM Activate and install
call venv\Scripts\activate.bat
pip install . -q
echo.
echo === Done ===
echo Run: nodus chat
echo.
