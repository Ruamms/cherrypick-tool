@echo off
chcp 65001 >nul
cd /d "%~dp0"
python gerar_exes.py %*
echo.
pause
