@echo off
rem Try Dos Ojos yourself, as a farmer signing up for the first time: an empty system,
rem a pretend phone in the browser, your own field and your own flight. From any folder.
rem
rem   try.cmd satellite          satellite only, Rio Grande Valley, as of 20 May 2025   (port 8090)
rem   try.cmd drone              satellite and drone, Purdue field 54, 11 July 2018     (port 8091)
rem   try.cmd thermal            satellite, drone and thermal, TERRA-REF, 20 May 2018   (port 8092)
rem
rem   try.cmd thermal update     what happens overnight: the satellite for the fields you
rem                              drew, and the drone half on the flights you uploaded
rem   try.cmd thermal reset      start that one over (the old one goes to old_demos)
setlocal
"%~dp0..\..\dosojos_sat\.venv\Scripts\python.exe" "%~dp0try_demo.py" %*
exit /b %ERRORLEVEL%
