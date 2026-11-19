@echo off
rem Double-click inside the Windows VM. Asks for admin (needed for the guest tools), then installs the apps.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-File','\"%~dp0Install-Apps.ps1\"'"
