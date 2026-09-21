@echo off
rem Put the SMS demo back the way setup_sms_demo.cmd left it, in a few seconds, from any
rem folder. Use it between farmers: first stop run_sms_demo.cmd (Ctrl+C in its window).
rem What the demo held is moved to Dos_Ojos\old_demos with the date and time, never deleted.
setlocal
set "DATA=%~dp0demo_data"
set "CLEAN=%~dp0demo_data_clean"
if not exist "%CLEAN%\sms\sms.sqlite" (
    echo There is no clean copy of the demo yet. setup_sms_demo.cmd makes one when it finishes.
    exit /b 1
)
for /f %%T in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"') do set "STAMP=%%T"
if not exist "%~dp0..\..\old_demos" mkdir "%~dp0..\..\old_demos"
if exist "%DATA%" (
    move "%DATA%" "%~dp0..\..\old_demos\sms-demo-%STAMP%" >nul || (
        echo Could not move the demo aside. Is run_sms_demo.cmd still running? Stop it first.
        exit /b 1
    )
)
robocopy "%CLEAN%" "%DATA%" /E /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 (
    echo Copying the clean demo failed.
    exit /b 1
)
echo The demo is back at its starting point. The one you used is in old_demos\sms-demo-%STAMP%.
echo Start it again with run_sms_demo.cmd
exit /b 0
