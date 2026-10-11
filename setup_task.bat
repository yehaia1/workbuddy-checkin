@echo off
rem Register Windows scheduled tasks for WorkBuddy daily check-in.
rem Runs at 09:00 and 21:00 daily (script is idempotent; extra runs are safe).
rem Remove/adjust times below as needed.

set BAT=%~dp0run_checkin.bat

schtasks /Create /TN "WorkBuddyCheckin_0900" /TR "%BAT%" /SC DAILY /ST 09:00 /F
schtasks /Create /TN "WorkBuddyCheckin_2100" /TR "%BAT%" /SC DAILY /ST 21:00 /F

echo.
echo Done. Verify with: schtasks /Query /TN "WorkBuddyCheckin_0900"
echo Remove with:  schtasks /Delete /TN "WorkBuddyCheckin_0900" /F
pause
