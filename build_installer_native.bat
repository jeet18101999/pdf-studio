@echo off
rem Builds the natively compiled PDF Studio installer (see installeruild_native.ps1)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installeruild_native.ps1"
pause
