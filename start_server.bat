@echo off
rem NailShot AI server - OpenAI API version (no GPU, no training needed)
rem Usage: start_server.bat            (real OpenAI generation, needs .env with OPENAI_API_KEY)
rem        start_server.bat --fake     (screen/mask check only, free)
rem        start_server.bat --lan      (allow phones on the same Wi-Fi)
cd /d "%~dp0"
set PYTHONPATH=
set PYTHONHOME=
set "PY=%~dp0venv_api\Scripts\python.exe"

if not exist "%PY%" (
  echo First run: creating a small Python environment in venv_api ...
  py -3 -m venv "%~dp0venv_api" 2>nul || python -m venv "%~dp0venv_api"
  if not exist "%PY%" (
    echo [ERROR] Python 3 was not found. Install it from https://www.python.org/downloads/ and try again.
    pause
    exit /b 1
  )
  "%PY%" -m pip install --upgrade pip
  "%PY%" -m pip install -r "%~dp0requirements_api.txt"
  if errorlevel 1 (
    echo [ERROR] Package install failed. Check the internet connection and run this file again.
    rmdir /s /q "%~dp0venv_api"
    pause
    exit /b 1
  )
)

if not exist "%~dp0.env" if "%OPENAI_API_KEY%"=="" (
  echo %* | find /i "--fake" >nul || (
    echo [ERROR] No API key. Copy .env.example to .env and put your OPENAI_API_KEY in it.
    echo         Or run:  start_server.bat --fake   to check the screen without the API.
    pause
    exit /b 1
  )
)

echo Starting NailShot AI server (OpenAI) ...
echo When you see the address, open it in Chrome. Close this window to stop the server.
"%PY%" "%~dp0nail_server.py" %*
pause
