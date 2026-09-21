@echo off
rem One-time set-up of the THERMAL demo, from PowerShell or cmd, from any folder.
rem A second, separate demo beside the Valley one: same bot, same texts, but on the only
rem field in the world we have a real thermal scan of. That field is REAL and PUBLIC and it
rem is not a farm: it is the TERRA-REF field scanner's strip of grain sorghum at the
rem Maricopa Agricultural Center in Arizona, planted 20 Apr 2018, with a FLIR camera run
rem over it on 20 May 2018 (public domain; see public_demo\terraref-sorghum-2018\SOURCE.md).
rem The farmer, Ana Example on a pretend 555 number, is MADE UP, and so are her watering
rem dates. The demo is pinned to the day of that scan, Sunday 20 May 2018, so its answers
rem never drift. Needs the internet, about 10 minutes, for satellite images, weather and
rem soil. Everything goes to examples\demo_data_thermal.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_thermal"
set "FIELDS=%~dp0..\..\public_demo\terraref-sorghum-2018\dosojos_sat\fields.geojson"
if exist "%DATA%\sms\sms.sqlite" (
    echo The thermal demo is already set up in %DATA%
    echo To start it over, move that folder somewhere else and run this again.
    exit /b 1
)
if not exist "%DATA%\sms" mkdir "%DATA%\sms"
> "%DATA%\sms\sms.env" (
    echo DOSOJOS_BANNER=EXAMPLE: a made-up farmer on a pretend 555 number, on a REAL public research field in Arizona - TERRA-REF, public domain. Not the Rio Grande Valley, and not our data.
    echo DOSOJOS_AS_OF=2018-05-20
)

rem Ana texts in and registers the field: option 3, satellite, drone and thermal camera.
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_ana.txt" --phone +19565550191 || exit /b 1

rem The team draws the field from the public outline and texts her the acres and map link.
"%PY%" -m dosojos_sms --data "%DATA%" outline F001 "%FIELDS%" --id PUBLIC-terraref-mac || exit /b 1

rem The daily run: satellite, weather and soil for 2016-2018, then the alerts.
"%PY%" -m dosojos_sms --data "%DATA%" daily --send --years 3 || exit /b 1

echo.
echo Ready. Start it with run_thermal_demo.cmd  (it opens on port 8082, so the Valley demo
echo can keep running on 8080 at the same time).
echo.
echo Then, as Ana: text DRONE for the upload link, send the thermal scan, and run
echo process_thermal_demo.cmd - or just run that script, which uses the same public frames.
exit /b 0
