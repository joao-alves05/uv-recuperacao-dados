@echo off
rem Abre o programa como administrador (necessario para ler o disco inteiro).
rem Rode de uma pasta que usuario comum nao consegue gravar (por exemplo dentro de Program Files):
rem quem grava aqui pode trocar o programa que roda com poder de administrador.
setlocal
net session >nul 2>&1
if %errorlevel% neq 0 (
  rem o caminho vai por variavel de ambiente: pasta com aspas simples ou outros simbolos nao vira comando
  set "UV_CMD=%~f0"
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath $env:ComSpec -ArgumentList '/c', ('\"' + $env:UV_CMD + '\"')"
  exit /b
)
rem resolve o python na hora, a partir da pasta do sistema (o cmd procura primeiro na pasta atual)
cd /d "%SystemRoot%"
set "PY="
for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
  echo Nao achei o Python no PATH. Instale em python.org e marque "Add python.exe to PATH".
  pause
  exit /b 1
)
cd /d "%~dp0"
"%PY%" iniciar.py --admin
