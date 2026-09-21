@echo off
rem The THERMAL demo's simulator, on port 8082 so the Valley demo can run at the same time.
rem Opens http://localhost:8082/sim in the browser; Ctrl+C in this window stops it.
rem Nothing is sent to real phones. The answers are as of Sun 20 May 2018, the day the
rem thermal camera ran, pinned in examples\demo_data_thermal\sms\sms.env.
rem Run setup_thermal_demo.cmd once first.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data_thermal"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The thermal demo is not set up yet: run setup_thermal_demo.cmd first.
    exit /b 1
)
"%PY%" -m dosojos_sms --data "%DATA%" serve --sim --open --port 8082
exit /b %ERRORLEVEL%
