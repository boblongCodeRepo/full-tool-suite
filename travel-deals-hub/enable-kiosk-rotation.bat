@echo off
rem Makes the train-times kiosk alternate with the travel deals screen.
rem Takes effect the next time the kiosk starts (sign-in, reboot, or stop-kiosk then the startup shortcut).
echo on> "%~dp0kiosk-rotation-on.txt"
echo Kiosk rotation turned ON. It starts with the kiosk's next launch.
pause
