@echo off
rem Abre o programa como administrador (necessario para ler o disco inteiro).
net session >nul 2>&1
if %errorlevel% neq 0 (
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)
cd /d "%~dp0"
python iniciar.py --admin
