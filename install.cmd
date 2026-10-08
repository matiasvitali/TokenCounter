@echo off
rem Doble clic: pide permisos de administrador y ejecuta scripts\install.ps1 (los argumentos se pasan solo si ya es admin).
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'" & exit /b)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
pause
