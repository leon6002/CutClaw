@echo off
setlocal
cd /d %~dp0

REM ==== 1. Activate conda env ====
call conda activate cutclaw 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" cutclaw
  ) else if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\anaconda3\Scripts\activate.bat" cutclaw
  ) else (
    echo [!] Could not find conda. Activate the cutclaw env manually, then run: python server\main.py
    pause
    exit /b 1
  )
)

REM ==== 2. Ensure backend deps ====
python -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo [*] Installing fastapi + uvicorn...
  pip install fastapi "uvicorn[standard]"
)

REM ==== 3. Build frontend if needed ====
REM Force rebuild:  start_web.bat rebuild
if /i "%~1"=="rebuild" goto :findnode
if exist "web\dist\index.html" goto :launch

:findnode
REM Locate a REAL node installation (not fnm's per-session shims, which break
REM outside their own shell). Checks fnm's store and Program Files.
set "NODEDIR="
for /f "usebackq delims=" %%N in (`powershell -NoProfile -ExecutionPolicy Bypass -File "scripts\find_node.ps1"`) do set "NODEDIR=%%N"

if not defined NODEDIR (
  echo [!] No Node.js installation found in fnm store or Program Files.
  echo     Build manually once from your dev terminal:
  echo       cd /d E:\apps\CutClaw\web ^&^& npm install ^&^& npm run build
  echo     then re-run this script.
  pause
  exit /b 1
)

set "NPMCLI=%NODEDIR%\node_modules\npm\bin\npm-cli.js"
if not exist "%NPMCLI%" (
  echo [!] Found node at %NODEDIR% but npm-cli.js is missing.
  pause
  exit /b 1
)

set "PATH=%NODEDIR%;%PATH%"
echo [*] Building frontend with: %NODEDIR%\node.exe
pushd web
call "%NODEDIR%\node.exe" "%NPMCLI%" install --no-audit --no-fund
if errorlevel 1 ( popd & pause & exit /b 1 )
call "%NODEDIR%\node.exe" "%NPMCLI%" run build
if errorlevel 1 ( popd & pause & exit /b 1 )
popd

:launch
start "" http://127.0.0.1:8765
python server\main.py
pause
