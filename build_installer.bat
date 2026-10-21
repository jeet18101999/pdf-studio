@echo off
rem One-click build of the PDF Studio installer (output: build\output\PDFStudio-Setup-1.0.0.exe)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\build.ps1"
pause
