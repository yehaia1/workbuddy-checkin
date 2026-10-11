@echo off
title WorkBuddy Daily Check-in
rem WorkBuddy daily check-in entry (Task Scheduler or double-click)
rem Uses WorkBuddy managed Python 3.13 (zero-dependency script, stdlib only)
setlocal
set "PY=C:\Users\denghaijian\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0workbuddy_checkin.py" %*
set "EXITCODE=%ERRORLEVEL%"

rem Keep the window open ONLY when launched by double-click, so the
rem result / error message is visible. Scheduled runs exit immediately
rem (cmdcmdline contains this bat path only in double-click context).
echo "%cmdcmdline%" | find /i "%~f0" >nul && (
    echo.
    echo [exit code %EXITCODE%] Press any key to close...
    pause >nul
)

endlocal & exit /b %EXITCODE%
