@echo off
REM Dispatcharr: refresh M3U, remap all channels, map Nufu Live-Games to slots 1-50.
REM Requires: Python on PATH (py launcher), .env in this folder with API settings.
REM
REM Task Scheduler example:
REM   Action: Start a program
REM   Program/script:  full path to this run_dispatcharr_daily.bat
REM   Start in:         folder that contains this repo (same folder as .env)
REM   (Or use cmd.exe /c with quoted path to this .bat if "Start in" is empty.)

setlocal EnableDelayedExpansion
cd /d "%~dp0"

if not exist "logs" mkdir "logs"

for /f "usebackq delims=" %%t in (`powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"`) do set "RUNSTAMP=%%t"
set "LOGFILE=%~dp0logs\run_!RUNSTAMP!.log"

echo [%date% %time%] Starting sync...>> "!LOGFILE!"
py -3 sync_streams_after_m3u.py --map-nufu-live-games >> "!LOGFILE!" 2>&1
set "EC=!ERRORLEVEL!"
echo [%date% %time%] Exit code: !EC!>> "!LOGFILE!"

exit /b !EC!
