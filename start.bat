@echo off
rem ============================================================
rem  adclean one-click launcher
rem  ASCII only: .bat is parsed with the console code page, which
rem  varies by machine. Keep non-ASCII text out of this file.
rem ============================================================
setlocal
cd /d "%~dp0"
title adclean - novel ad cleaner

echo ============================================================
echo    adclean - Chinese novel TXT ad cleaner
echo    high-recall audit + high-precision repair + LLM review
echo ============================================================
echo.

rem ---------- 1. locate Python (py -3, then python / python3) ----------
set "PY="
where py >nul 2>&1
if not errorlevel 1 (
  py -3 -c "import sys;raise SystemExit(0 if sys.version_info>=(3,9) else 1)" >nul 2>&1
  if not errorlevel 1 set "PY=py -3"
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
  echo.
  echo   IMPORTANT: on the first install screen, tick
  echo     [x] Add python.exe to PATH
  echo.
  echo   Then close this window and double-click start.bat again.
  echo.
  pause
  exit /b 1
)

%PY% -c "import sys;print('  Python  '+sys.version.split()[0])"
echo.

rem ---------- 2. make sure the adclean package sits next to this file ----------
%PY% -c "import adclean" >nul 2>&1
if errorlevel 1 (
  echo [ERROR] the "adclean" package was not found.
  echo   Keep start.bat in the SAME folder as the adclean folder.
  echo   Current folder: %CD%
  echo.
  pause
  exit /b 1
)

rem ---------- 3. warn if the n-gram corpus cache is missing ----------
set "HAVELM=0"
if exist ".adclean-lm-*.json" set "HAVELM=1"
if exist ".adclean-lm.json" set "HAVELM=1"
if "%HAVELM%"=="0" (
  echo [WARN] clean n-gram cache .adclean-lm-*.json not found.
  echo   The audit stage needs it, otherwise jobs will fail.
  echo   Put the cache files in: %CD%
  echo.
)

rem ---------- 4. ignore a dropped file/folder (the UI does not need it) ----------
set "ARGS=%*"
set "FIRST=%~1"
if not "%FIRST%"=="" if not "%FIRST:~0,1%"=="-" (
  echo [NOTE] argument "%FIRST%" ignored.
  echo        Choose files inside the web UI after it opens.
  echo.
  set "ARGS="
)

rem ---------- 5. launch ----------
echo Starting local web UI, the browser will open automatically...
echo Close this window to stop the server.
echo.
%PY% -m adclean.server %ARGS%
set "RC=%errorlevel%"

if not "%RC%"=="0" (
  echo.
  echo [server exited, code=%RC%]
  pause
)
exit /b %RC%
