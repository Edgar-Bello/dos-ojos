@echo off
rem The DRONE demo's simulator, on port 8081 so the Valley demo can run at the same time.
rem Opens http://localhost:8081/sim in the browser; Ctrl+C in this window stops it.
rem Nothing is sent to real phones. The answers are as of Wed 11 July 2018, the day after the
rem drone flew, pinned in examples\demo_data_drone\sms\sms.env.
rem Run setup_drone_demo.cmd once first.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_drone"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The drone demo is not set up yet: run setup_drone_demo.cmd first.
    exit /b 1
)
"%PY%" -m dosojos_sms --data "%DATA%" serve --sim --open --port 8081
exit /b %ERRORLEVEL%
