@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist config.json (
  echo [!] Нет config.json - скопируйте config.example.json.
  pause
  exit /b 1
)
if not exist .venv (
  python -m venv .venv || goto :err
  .venv\Scripts\python -m pip install -r requirements.txt || goto :err
)
.venv\Scripts\python server.py
goto :eof
:err
echo [x] Ошибка установки/запуска.
pause
