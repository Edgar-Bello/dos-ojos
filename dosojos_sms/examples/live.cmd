@echo off
rem Dos Ojos on real phones: a live system on the real date, a tunnel to it, and your
rem Twilio number pointed at it. From any folder.
rem
rem   live.cmd            start it all (Ctrl+C stops it)
rem   live.cmd update     the daily run by hand: satellite, weather, soil, alerts
setlocal
"%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe" "%~dp0live_demo.py" %*
exit /b %ERRORLEVEL%
