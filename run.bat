@echo off
REM Double-click to start the Schedule vs. Timesheet app.
cd /d "%~dp0"
where pyw >nul 2>nul && (start "" pyw -m timecompare.gui & exit /b)
where pythonw >nul 2>nul && (start "" pythonw -m timecompare.gui & exit /b)
echo Python was not found. Install it from https://www.python.org/downloads/ (check "Add to PATH").
pause
