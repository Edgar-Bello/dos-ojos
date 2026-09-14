@echo off
rem The SMS simulator on the EXAMPLE farmers in examples\demo_data, from any folder.
rem Opens http://localhost:8080/sim in the browser; Ctrl+C in this window stops it.
rem Nothing is sent to real phones. Run setup_sms_demo.cmd once first.
setlocal
set "PY=%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe"
set "DATA=%~dp0demo_data"
if not exist "%DATA%\sms\sms.sqlite" (
    echo The demo is not set up yet: run setup_sms_demo.cmd first.
    exit /b 1
)
"%PY%" -m dosojos_sms --data "%DATA%" serve --sim --open
exit /b %ERRORLEVEL%
