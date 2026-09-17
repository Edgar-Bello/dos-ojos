@echo off
rem After a farmer signs up their OWN field in the simulator and taps its corners on the map
rem link, run this (from any folder, the simulator can keep running) to fetch this season's
rem satellite pictures, weather and soil for it: about 5 minutes. Then AGUA, ETAPA, PULGON
rem and PORQUE answer for their field too. It only fetches the pinned season, so the page
rem has no "normal for this field" band until the full history is fetched overnight.
rem Needs the internet.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The demo is not set up yet: run setup_sms_demo.cmd first.
    exit /b 1
)
"%PY%" -m dosojos_sms --data "%DATA%" daily --send --years 1 --skip-baseline
exit /b %ERRORLEVEL%
