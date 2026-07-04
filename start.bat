@echo off
cd /d %~dp0
call conda activate cutclaw 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\miniconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\miniconda3\Scripts\activate.bat" cutclaw
  ) else if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
    call "%USERPROFILE%\anaconda3\Scripts\activate.bat" cutclaw
  ) else (
    echo Could not find conda. Activate the cutclaw env manually, then run: streamlit run app.py
    pause
    exit /b 1
  )
)
streamlit run app.py
pause
