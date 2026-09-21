@echo off
rem The team step of the THERMAL demo: take what Ana uploaded and run the drone half on it.
rem Stitches the thermal frames into one map of canopy temperature (about 3 minutes for a
rem full scan), scores the warm patches, and joins the result to the satellite side. Then
rem Ana can text PORQUE / WHY and the page comes back with the thermal map on it.
rem With nothing uploaded yet it runs on the same public frames the demo is built on, in
rem public_demo\terraref-sorghum-2018. From PowerShell or cmd, from any folder.
setlocal
set "PY=%~dp0..\..\dosojos_drone\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_thermal"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The thermal demo is not set up yet: run setup_thermal_demo.cmd first.
    exit /b 1
)
"%PY%" "%~dp0process_flight.py" --data "%DATA%"
exit /b %ERRORLEVEL%
