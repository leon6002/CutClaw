@echo off
setlocal
cd /d %~dp0

REM ==== Backend API only - for dev use with: cd web ^&^& pnpm dev ====

REM Free port 8765 first: kill any stale server still holding it, so running
REM this always does a CLEAN restart (no port-in-use, no leftover process).
echo [*] Freeing port 8765 (killing any stale server)...
for /f "tokens=5" %%P in ('netstat -ano ^| findstr ":8765" ^| findstr "LISTENING"') do (
  echo     killing PID %%P
  taskkill /F /PID %%P >nul 2>&1
)

REM ==== Activate conda env ====
call conda activate cutclaw 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" cutclaw
  ) else if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\anaconda3\Scripts\activate.bat" cutclaw
  ) else (
    echo [!] Could not find conda. Activate cutclaw manually, then run: python server\main.py
    pause
    exit /b 1
  )
)

python -c "import fastapi, uvicorn" 2>nul
if errorlevel 1 (
  echo [*] Installing fastapi + uvicorn...
  pip install fastapi "uvicorn[standard]"
)

REM Run WITHOUT --reload on purpose: uvicorn's reloader runs a multiprocessing
REM worker that breaks torch's DLL loading on Windows, and only watches server/
REM so src/ changes never reload anyway. To pick up code changes, just re-run
REM this script - it kills the old server above and starts fresh.
echo [*] API running at http://127.0.0.1:8765  -  frontend: cd web ^&^& pnpm dev
python server\main.py
pause
