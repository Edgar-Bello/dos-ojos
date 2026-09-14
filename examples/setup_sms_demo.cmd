@echo off
rem One-time set-up of the SMS demo, from PowerShell or cmd, from any folder.
rem EXAMPLE data only: a made-up farmer (Juan Ejemplo, pretend number 956-555-0123)
rem registers the placeholder field "Mercedes North 40" by text; then the daily run
rem fetches its satellite images, weather and soil (needs the internet, about 3 minutes).
rem Everything goes to examples\demo_data, apart from the real farm_data.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DRONE=%~dp0..\..\dosojos_drone\.venv\Scripts\python.exe"
set "GROUND=%~dp0..\..\dosojos_drone\out\chm-water\terrain.json"
set "DATA=%~dp0demo_data"
if exist "%DATA%\sms\sms.sqlite" (
    echo The demo is already set up in %DATA%
    echo To start it over, delete that folder and run this again.
    exit /b 1
)
if not exist "%DATA%\sms" mkdir "%DATA%\sms"
> "%DATA%\sms\sms.env" echo DOSOJOS_BANNER=EXAMPLE: made-up farmers on pretend 555 numbers. Satellite, weather and soil are real.
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_farmer.txt" || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" outline F001 "%~dp0demo_field.geojson" --quiet || exit /b 1

rem The drone half's SYNTHETIC ground test flight over this same placeholder field, so
rem AGUA can show ground advice. Skipped if that test flight was never made here.
if exist "%GROUND%" (
    "%DRONE%" -m dosojos_drone --workspace "%DATA%\dosojos_drone" register F001-SYNTHETIC-ground --field F001 --date 2026-09-10 --crop "grain sorghum" --source "Synthetic: dosojos_drone tools/make_synthetic_field.py --water-pattern" --notes "SYNTHETIC: the drone half's chm-water test flight, copied for the SMS demo" || exit /b 1
    if not exist "%DATA%\dosojos_drone\out\F001-SYNTHETIC-ground" mkdir "%DATA%\dosojos_drone\out\F001-SYNTHETIC-ground"
    copy /y "%GROUND%" "%DATA%\dosojos_drone\out\F001-SYNTHETIC-ground\terrain.json" >nul || exit /b 1
)

"%PY%" -m dosojos_sms --data "%DATA%" daily || exit /b 1
echo.
echo Ready. Start the simulator with run_sms_demo.cmd
exit /b 0
