@echo off
rem Re-run the Purdue public-data demo from PowerShell or cmd. FREE PUBLIC DATA, NOT OURS.
rem "bash" in PowerShell is WSL's, which cannot run the Windows tools, so this uses Git Bash.
setlocal
set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not exist "%GITBASH%" (
    echo Git Bash was not found at "%GITBASH%".
    echo Install Git for Windows, or open a Git Bash window and run run_demo.sh there.
    exit /b 1
)
set "SCRIPT=%~dp0run_demo.sh"
"%GITBASH%" "%SCRIPT:\=/%"
exit /b %ERRORLEVEL%
