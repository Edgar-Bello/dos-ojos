@echo off
rem One-time set-up of the DRONE demo, from PowerShell or cmd, from any folder.
rem A third demo beside the Valley one and the thermal one: same bot, same texts, but on the
rem one real set of drone photos of sorghum we have. That field is REAL and PUBLIC and it is
rem not a farm: it is Purdue University's field 54 in Indiana, 72 plots of 18 sorghum
rem hybrids sown 8 May 2018 and flown with their own drone and laser on 10 July 2018
rem (CC0; see public_demo\purdue-sorghum-2018\SOURCE.md). The farmer, Dan Example on a
rem pretend 555 number, is MADE UP. Purdue's readme records no irrigation, so rainfed is our
rem assumption. The demo is pinned to the day after that flight, Wed 11 July 2018, so its
rem answers never drift. Needs the internet, about 15 minutes, for satellite images,
rem weather and soil. Everything goes to examples\demo_data_drone.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_drone"
set "FIELDS=%~dp0..\..\public_demo\purdue-sorghum-2018\dosojos_sat\fields.geojson"
if exist "%DATA%\sms\sms.sqlite" (
    echo The drone demo is already set up in %DATA%
    echo To start it over, move that folder somewhere else and run this again.
    exit /b 1
)
if not exist "%DATA%\sms" mkdir "%DATA%\sms"
> "%DATA%\sms\sms.env" (
    echo DOSOJOS_BANNER=EXAMPLE: a made-up farmer on a pretend 555 number, on a REAL public research field in Indiana - Purdue University, CC0. Not the Rio Grande Valley, and not our data.
    echo DOSOJOS_AS_OF=2018-07-11
)

rem Dan texts in and registers the field: option 2, satellite and his own drone.
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_dan.txt" --phone +19565550178 || exit /b 1

rem The team draws the field from the public outline and texts him the acres and map link.
"%PY%" -m dosojos_sms --data "%DATA%" outline F001 "%FIELDS%" --id PUBLIC-purdue-f54 || exit /b 1

rem The daily run: satellite, weather and soil for 2016-2018, then the alerts.
"%PY%" -m dosojos_sms --data "%DATA%" daily --send --years 3 || exit /b 1

echo.
echo Ready. Start it with run_drone_demo.cmd  (it opens on port 8081, so the other demos
echo can keep running at the same time).
echo.
echo Then, as Dan: text DRONE for the upload link, send the flight, and run
echo process_drone_demo.cmd - or just run that script, which uses the same public files.
exit /b 0
