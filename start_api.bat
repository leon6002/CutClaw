@echo off
setlocal
cd /d %~dp0

REM ==== Backend API only - for dev use with: cd web ^&^& pnpm dev ====

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

python -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo [*] Installing fastapi + uvicorn...
  pip install fastapi "uvicorn[standard]"
)

REM Dev hot-reload:  start_api.bat reload   - auto-restarts on server code changes
echo [*] API running at http://127.0.0.1:8765  -  frontend dev server: cd web ^&^& pnpm dev
if /i "%~1"=="reload" (
  python server\main.py --reload
) else (
  python server\main.py
)
pause
