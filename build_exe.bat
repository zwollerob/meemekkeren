@echo off
rem Bouwt dist\Meezinger.exe (één bestand, geen Python nodig op de doel-pc)
cd /d "%~dp0"
if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv
)
.venv\Scripts\python -m pip install -r requirements.txt pyinstaller
.venv\Scripts\pyinstaller --noconfirm --onefile --windowed --name Meezinger ^
    --collect-all shazamio --collect-all shazamio_core --collect-all soundcard ^
    meezinger.py
echo.
echo Klaar: dist\Meezinger.exe
pause
