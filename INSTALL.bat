@echo off
REM ============================================================
REM  Rating Watchlist - one-time setup
REM  Double-click this file. Nothing else is needed.
REM ============================================================
setlocal
cd /d "%~dp0"
title Rating Watchlist - Setup

echo.
echo  ============================================
echo    Rating Watchlist - first-time setup
echo  ============================================
echo.

REM -- 1. find Python -------------------------------------------------
set "PY="
python --version >nul 2>&1 && set "PY=python"
if not defined PY py -3 --version >nul 2>&1 && set "PY=py -3"

if not defined PY (
  echo  [X] Python was not found on this computer.
  echo.
  echo      Install Python 3.13 ^(64-bit^) from:
  echo        https://www.python.org/downloads/windows/
  echo.
  echo      IMPORTANT: tick "Add python.exe to PATH" on the first
  echo      screen of the installer, then run this file again.
  echo.
  pause
  exit /b 1
)

for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo  [1/3] Python %PYVER% found.

REM -- 2. install the packages ---------------------------------------
echo  [2/3] Installing packages (offline, from the wheels folder)...
echo.
%PY% -m pip install --no-index --find-links wheels -r requirements-lock.txt --quiet
if errorlevel 1 (
  echo.
  echo  [!] The offline install did not succeed. Trying the internet...
  echo.
  %PY% -m pip install -r requirements.txt --quiet
  if errorlevel 1 (
    echo.
    echo  [X] Could not install the packages.
    echo.
    echo      If the message above mentions "not a supported wheel on
    echo      this platform", the bundle in wheels\ was built for a
    echo      different Python version than %PYVER%. Ask for a rebuilt
    echo      bundle - the command is in README.md, Step 5b.
    echo.
    pause
    exit /b 1
  )
)
echo       Packages installed.

REM -- 3. prove it works, without touching the network ---------------
echo  [3/3] Checking the installation...
echo.
%PY% tests\test_extreme_names.py >nul 2>&1
if errorlevel 1 goto :checkfailed
%PY% tests\test_normalize.py >nul 2>&1
if errorlevel 1 goto :checkfailed
%PY% tests\test_matching.py >nul 2>&1
if errorlevel 1 goto :checkfailed

echo       All checks passed.
echo.
echo  ============================================
echo    Setup complete.
echo.
echo    Now double-click  START.bat  to run it.
echo  ============================================
echo.
pause
exit /b 0

:checkfailed
echo.
echo  [X] The installation checks did not pass.
echo      Run this to see the details:
echo         %PY% tests\test_extreme_names.py
echo.
pause
exit /b 1
