@echo off
rem ============================================================
rem  adclean one-click launcher
rem  ASCII only + CRLF: .bat is parsed with the console code page.
rem ============================================================
setlocal
cd /d "%~dp0"
title adclean - novel ad cleaner

echo ============================================================
echo    adclean - Chinese novel TXT ad cleaner
echo    36-dim locator + high-recall audit + interactive review
echo ============================================================
echo.

rem ---------- 1. locate Python (.venv -> venv -> py -3 -> python) ----------
set "PY="
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=.venv\Scripts\python.exe"
)
if not defined PY if exist "venv\Scripts\python.exe" (
  "venv\Scripts\python.exe" -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=venv\Scripts\python.exe"
)
if not defined PY (
  where py >nul 2>&1
  if not errorlevel 1 (
    py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=py -3"
  )
)
if not defined PY (
  for %%C in (python python3) do (
    if not defined PY (
      %%C -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
      if not errorlevel 1 set "PY=%%C"
    )
  )
)

if not defined PY (
  echo [ERROR] Python 3.9 or newer was not found.
  echo.
  echo   Install it from:
  echo     https://www.python.org/downloads/windows/
  echo   Or create a virtual environment:
  echo     python -m venv .venv
  echo.
  pause
  exit /b 1
)

for /f "delims=" %%V in ('%PY% -c "import sys;print(sys.version.split()[0])"') do set "PY_VER=%%V"
echo   Python  : %PY_VER% (%PY%)

rem ---------- 2. check adclean package and core dependencies ----------
%PY% -c "import adclean" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] the "adclean" package was not found.
  echo   Keep start.bat in the SAME folder as the adclean package.
  echo   Current folder: %CD%
  echo.
  pause
  exit /b 1
)

%PY% -c "import numpy, lightgbm" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] missing required dependencies: numpy / lightgbm.
  echo.
  echo   Please install dependencies first:
  echo     %PY% -m pip install -r requirements.txt
  echo   Or:
  echo     %PY% -m pip install -e .
  echo.
  pause
  exit /b 1
)

rem ---------- 3. language model status ----------
set "HAVELM=0"
if exist ".adclean-lm-*.json" set "HAVELM=1"
if exist ".adclean-lm.json" set "HAVELM=1"
if "%HAVELM%"=="1" (
  echo   LM Mode : external n-gram cache detected
) else (
  echo   LM Mode : adaptive auto-fit per book ^(no external cache required^)
)
echo.

rem ---------- 4. ignore dropped file/folder path if passed directly ----------
set "ARGS=%*"
set "FIRST=%~1"
if not "%FIRST%"=="" if not "%FIRST:~0,1%"=="-" (
  echo [NOTE] argument "%FIRST%" ignored in browser mode.
  echo        Drag or select files directly inside the Web UI.
  echo.
  set "ARGS="
)

rem ---------- 5. launch foreground server in current console ----------
echo Starting local Web UI, the browser will open automatically...
echo Press Ctrl+C or close this window to stop the server immediately.
echo.
set "PYTHONIOENCODING=utf-8"
%PY% -m adclean.server %ARGS%
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo.
  echo [server exited with error code=%RC%]
  pause
)
exit /b %RC%
