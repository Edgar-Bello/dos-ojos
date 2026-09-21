@echo off
rem Dos Ojos SMS from PowerShell or cmd, from any folder:  sms.cmd <command> [options]
rem It runs in the satellite half's Python, since Windows Smart App Control blocks the
rem pip-built .exe launchers. Try:  sms.cmd --help
"%~dp0..\dosojos_sat\.venv\Scripts\python.exe" -m dosojos_sms %*
exit /b %ERRORLEVEL%
