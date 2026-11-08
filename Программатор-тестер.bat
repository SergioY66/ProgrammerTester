@echo off
chcp 65001 >nul
cd /d "%~dp0pc"
python -c "import serial" 2>nul || python -m pip install pyserial
start "" pythonw gui.py
