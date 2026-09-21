@echo off
rem Re-run the water example from PowerShell or cmd, from any folder.
rem EXAMPLE: placeholder fields, a made-up field log, synthetic drone flights (chm-water,
rem chm-trees). Real weather (gridMET), soil (USDA SSURGO) and imagery (Sentinel-2).
rem Pinned to 12 Sep 2026 so the numbers are the same every time it is shown.
setlocal
set "ROOT=%~dp0..\.."
set "SAT=%ROOT%\dosojos_sat\.venv\Scripts\python.exe -m dosojos_sat"
set "DRONE=%ROOT%\dosojos_drone\.venv\Scripts\python.exe -m dosojos_drone"
%SAT% water --as-of 2026-09-12 --log "%~dp0field_log_EXAMPLE.csv" --banner "EXAMPLE FIELD LOG, NOT REAL RECORDS  -  placeholder fields; weather gridMET, soil USDA SSURGO, imagery Sentinel-2" || exit /b 1
%DRONE% terrain chm-water || exit /b 1
%DRONE% join
exit /b %ERRORLEVEL%
