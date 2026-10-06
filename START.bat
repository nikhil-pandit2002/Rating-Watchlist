@echo off
REM ============================================================
REM  Rating Watchlist - start the application
REM  Double-click this file, then use the browser window.
REM ============================================================
setlocal
cd /d "%~dp0"
title Rating Watchlist - running (close this window to stop)

set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"

if not defined PY (
  echo  [X] Python was not found. Run INSTALL.bat first.
  pause
  exit /b 1
)

REM A quick check that setup has been done, so the failure is
REM explained rather than a wall of import errors.
%PY% -c "import flask, pandas, requests" >nul 2>&1
if errorlevel 1 (
  echo  [X] The required packages are not installed yet.
  echo      Double-click INSTALL.bat first, then try again.
  echo.
  pause
  exit /b 1
)

echo.
echo  ============================================
echo    Rating Watchlist is starting...
echo.
echo    Your browser will open at:
echo      http://127.0.0.1:5000
echo.
echo    KEEP THIS WINDOW OPEN while you use it.
echo    Close it (or press Ctrl+C) to stop.
echo  ============================================
echo.

REM Give the server a moment before the browser opens, otherwise the
REM page can load before Flask is listening and show a refused error.
start "" /b cmd /c "timeout /t 3 /nobreak >nul & start http://127.0.0.1:5000"

%PY% webapp\app.py

echo.
echo  The application has stopped.
pause
