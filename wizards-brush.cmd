@echo off
rem The Wizard's Brush on native Windows. Double-click, or run from cmd/PowerShell:
rem   wizards-brush.cmd setup [-Deps full^|test] [-SkipModels]
rem   wizards-brush.cmd start [-Foreground] [-NoBrowser] [-Build] [-Port N]
rem   wizards-brush.cmd stop | restart | status | dev | browser | doctor | test
rem The execution policy is bypassed for these project scripts only, in this
rem process; nothing is changed for the machine or the user. No admin rights.
setlocal
set "WB_ROOT=%~dp0"
set "WB_PS=powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File"
set "WB_CMD=%~1"
if "%WB_CMD%"=="" set "WB_CMD=start"
shift
set "WB_ARGS="
:collect
if "%~1"=="" goto run
set "WB_ARGS=%WB_ARGS% %1"
shift
goto collect
:run
if /i "%WB_CMD%"=="setup"   %WB_PS% "%WB_ROOT%scripts\setup.ps1" %WB_ARGS% & goto done
if /i "%WB_CMD%"=="start"   %WB_PS% "%WB_ROOT%scripts\start.ps1" %WB_ARGS% & goto done
if /i "%WB_CMD%"=="stop"    %WB_PS% "%WB_ROOT%scripts\stop.ps1" %WB_ARGS% & goto done
if /i "%WB_CMD%"=="restart" (
  %WB_PS% "%WB_ROOT%scripts\stop.ps1"
  %WB_PS% "%WB_ROOT%scripts\start.ps1" %WB_ARGS%
  goto done
)
if /i "%WB_CMD%"=="dev"     %WB_PS% "%WB_ROOT%scripts\dev.ps1" %WB_ARGS% & goto done
if /i "%WB_CMD%"=="browser" %WB_PS% "%WB_ROOT%scripts\browser.ps1" %WB_ARGS% & goto done
if /i "%WB_CMD%"=="status"  %WB_PS% "%WB_ROOT%scripts\run-python.ps1" scripts\serve.py status & goto done
if /i "%WB_CMD%"=="doctor"  %WB_PS% "%WB_ROOT%scripts\run-python.ps1" scripts\doctor.py & goto done
if /i "%WB_CMD%"=="test"    %WB_PS% "%WB_ROOT%scripts\run-python.ps1" -m pytest tests %WB_ARGS% & goto done
echo Unknown command "%WB_CMD%". Use setup, start, stop, restart, status, dev, browser, doctor or test.
exit /b 2
:done
exit /b %ERRORLEVEL%
