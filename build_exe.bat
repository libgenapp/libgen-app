@echo off
REM Build a standalone LibGen.exe. Run this on WINDOWS (not WSL). `python` should be your
REM Windows Python/Anaconda. PyInstaller cannot cross-compile. lgsearch.py sits next to this
REM file and is imported, so PyInstaller bundles it automatically.
cd /d "%~dp0"

REM Anaconda keeps DLLs (libbz2, liblzma, libssl, libcrypto...) in Library\bin, which PyInstaller
REM misses unless it's on PATH -> the exe then dies with "libbz2.dll not found". Put the Python
REM install's DLL folders on PATH so they get bundled, whatever shell this runs from.
for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.dirname(sys.executable))"') do set PYDIR=%%i
if "%PYDIR%"=="" goto :err
set PATH=%PYDIR%;%PYDIR%\Library\bin;%PYDIR%\DLLs;%PATH%

python -m pip install --upgrade pyqt5 pyinstaller || goto :err

REM always build clean so a stale build\ or .spec can't carry old settings
rmdir /s /q build dist 2>nul
del /q LibGen.spec 2>nul

pyinstaller --onefile --windowed --name LibGen --icon icon.ico ^
  --paths "%PYDIR%\Library\bin" --paths "%PYDIR%\DLLs" libgen_app.py || goto :err
echo.
echo Done. Your app is at:  dist\LibGen.exe
goto :eof
:err
echo.
echo Build failed. Make sure `python` points at your Windows Python and try again.
exit /b 1
