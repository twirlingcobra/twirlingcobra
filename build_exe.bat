@echo off
REM Builds a single TimeCompare.exe in the "dist" folder (no Python needed to run it).
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller openpyxl || goto :err
py -m PyInstaller --noconfirm --onefile --windowed --name TimeCompare app.py || goto :err
echo.
echo Done: dist\TimeCompare.exe   (the database timecompare.db is created next to the .exe)
pause
exit /b
:err
echo Build failed.
pause
