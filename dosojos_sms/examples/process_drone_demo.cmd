@echo off
rem The team step of the DRONE demo: take what Dan uploaded and run the drone half on it.
rem Imports the finished maps (orthophoto and the two laser scans), builds the canopy model,
rem measures every stretch of row, flags them, and maps the lie of the land: a few minutes.
rem Then Dan can text WHY and the page comes back with all of it on one file.
rem With nothing uploaded yet it runs on the same public flight the demo is built on, in
rem public_demo\purdue-sorghum-2018. From PowerShell or cmd, from any folder.
setlocal
set "PY=%~dp0..\..\dosojos_drone\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_drone"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The drone demo is not set up yet: run setup_drone_demo.cmd first.
    exit /b 1
)
"%PY%" "%~dp0process_flight.py" --data "%DATA%"
exit /b %ERRORLEVEL%
