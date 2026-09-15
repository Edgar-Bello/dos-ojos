@echo off
rem One-time set-up of the SMS demo, from PowerShell or cmd, from any folder.
rem Four REAL Rio Grande Valley fields from public USDA data - cotton, corn, grain sorghum
rem and citrus - and three MADE-UP farmers on pretend 555 numbers who text about them:
rem Juan Ejemplo and Maria Ejemplo in Spanish, Mary Example in English. Their planting and
rem watering dates are invented to match what the satellite saw. The demo is pinned to
rem Tue 20 May 2025 so its answers never drift. Needs the internet, about 10 minutes, for
rem the satellite images, weather, soil, and the government lidar ground map of the two
rem furrow-watered fields. Everything goes to examples\demo_data, apart from farm_data.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DRONE=%~dp0..\..\dosojos_drone\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data"
set "FIELDS=%~dp0demo_fields.geojson"
if exist "%DATA%\sms\sms.sqlite" (
    echo The demo is already set up in %DATA%
    echo To start it over, move that folder somewhere else and run this again.
    exit /b 1
)
if not exist "%DATA%\sms" mkdir "%DATA%\sms"
> "%DATA%\sms\sms.env" (
    echo DOSOJOS_BANNER=EXAMPLE: made-up farmers on pretend 555 numbers, on real fields. Satellite, weather, soil and lidar are real public data.
    echo DOSOJOS_AS_OF=2025-05-20
)

rem The farmers text in, one after the other: F001 cotton and F002 corn are Juan's,
rem F003 sorghum is Maria's, F004 citrus is Mary's.
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_juan.txt" --phone +19565550123 || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_maria.txt" --phone +19565550142 || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" replay "%~dp0demo_mary.txt" --phone +19565550187 || exit /b 1

rem The team draws each field from the public outlines and texts the farmer its acres and
rem map link; a farmer can also tap the corners on that map.
"%PY%" -m dosojos_sms --data "%DATA%" outline F001 "%FIELDS%" --id PUBLIC-rgv-cotton-1 || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" outline F002 "%FIELDS%" --id PUBLIC-rgv-corn-1 || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" outline F003 "%FIELDS%" --id PUBLIC-rgv-sorghum-1 || exit /b 1
"%PY%" -m dosojos_sms --data "%DATA%" outline F004 "%FIELDS%" --id PUBLIC-rgv-citrus-1 || exit /b 1

rem The daily run: satellite, weather and soil, then the alerts, kept in the simulator.
"%PY%" -m dosojos_sms --data "%DATA%" daily --send || exit /b 1

rem The ground under the two furrow-watered fields, from USGS 3DEP airborne lidar flown in
rem 2019 (public domain) instead of a drone. Skipped with a note if it cannot be reached.
for %%F in (F001 F002) do (
    "%DRONE%" -m dosojos_drone --workspace "%DATA%\dosojos_drone" lidar %%F-lidar --field %%F && "%DRONE%" -m dosojos_drone --workspace "%DATA%\dosojos_drone" terrain %%F-lidar || echo NOTE: no lidar ground for %%F, the demo works without it.
)

echo.
echo Ready. Start the simulator with run_sms_demo.cmd
exit /b 0
