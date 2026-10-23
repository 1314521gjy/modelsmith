@echo off
setlocal EnableExtensions
set "MS_ROOT=%~dp0"
set "MS_PORT=8765"
if defined START_CORE_PORT set "MS_PORT=%START_CORE_PORT%"
set "MS_DATA=C:\ModelSmith\state"
if defined MODELSMITH_STATE set "MS_DATA=%MODELSMITH_STATE%"
set "MS_LOG_DIR=%MS_DATA%\logs"
set "MS_LOG=%MS_LOG_DIR%\launcher.log"
rem default browser URL: http://127.0.0.1:8765/

set "MS_RUNTIME=%MS_ROOT%dist\runtime"
if defined MODELSMITH_RUNTIME set "MS_RUNTIME=%MODELSMITH_RUNTIME%"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=C:\ModelSmith\runtime"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=%MS_ROOT%dist\runtime"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=%MS_ROOT%runtime"
if not exist "%MS_RUNTIME%\python\python.exe" (
  echo RUNTIME MISSING.
  echo Looked for: C:\ModelSmith\runtime, dist\runtime, runtime
  echo Copy the runtime folder to C:\ModelSmith\runtime or set MODELSMITH_RUNTIME.
  echo See docs\QUICKSTART-*.md
  pause
  exit /b 1
)

set "MS_PY=%MS_RUNTIME%\python\python.exe"
set "MS_CORE=%MS_ROOT%core\modelsmith_core.py"
set "MS_WIRE=%MS_ROOT%core\assets\ms-wire.js"
set "MS_DIFF=%MS_ROOT%adapters\diffsynth_adapter.py"
rem Do NOT set PYTHONHOME/PYTHONPATH: the embedded runtime locates its own
rem stdlib via python312._pth, and exporting PYTHONHOME breaks every child
rem process that is not that exact interpreter (adapters / other pythons / stubs).
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"

if not exist "%MS_DATA%" (
  mkdir "%MS_DATA%" 2>nul
  if not exist "%MS_DATA%" (
    echo DATA DIR CANNOT BE CREATED: %MS_DATA%
    pause
    exit /b 1
  )
)
if not exist "%MS_LOG_DIR%" (
  mkdir "%MS_LOG_DIR%" 2>nul
  if not exist "%MS_LOG_DIR%" (
    echo LOG DIR CANNOT BE CREATED: %MS_LOG_DIR%
    pause
    exit /b 1
  )
)

rem ---- is the port already listening? -------------------------------------
set "HAS_LISTENER=0"
for /f %%c in ('powershell -NoProfile -ExecutionPolicy Bypass -Command "(Get-NetTCPConnection -LocalPort %MS_PORT% -State Listen -ErrorAction SilentlyContinue | Measure-Object).Count"') do set "HAS_LISTENER=%%c"
if "%HAS_LISTENER%"=="0" goto :start

rem ---- is it OUR core?  pid alive?  code stale? ---------------------------
if exist "%MS_LOG%.state" del "%MS_LOG%.state" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "$s='%MS_LOG%.state'; $h=$null; try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:%MS_PORT%/health' -TimeoutSec 2 } catch {}; if(-not $h -or -not $h.ok -or -not $h.pid){ 'STATUS=FOREIGN' | Set-Content -Path $s -Encoding ascii; exit 0 }; $proc=Get-Process -Id $h.pid -ErrorAction SilentlyContinue; if(-not $proc){ 'STATUS=DEAD' | Set-Content -Path $s -Encoding ascii; exit 0 }; try { $start=$proc.StartTime } catch { $start=(Get-Date).AddSeconds(-[double]$h.uptime_s) }; $stale=0; foreach($f in @('%MS_CORE%','%MS_WIRE%','%MS_DIFF%')){ $it=Get-Item -LiteralPath $f -ErrorAction SilentlyContinue; if($it -and $it.LastWriteTime -gt $start){ $stale=1 } }; $lines=@(); $lines += 'STATUS=OURS'; $lines += ('PID=' + [string]$h.pid); $lines += ('STALE=' + [string]$stale); $lines | Set-Content -Path $s -Encoding ascii"
set "PSTATUS="
set "POLD="
set "PSTALE="
if exist "%MS_LOG%.state" (
  for /f "tokens=2 delims==" %%a in ('findstr /b "STATUS=" "%MS_LOG%.state"') do set "PSTATUS=%%a"
  for /f "tokens=2 delims==" %%a in ('findstr /b "PID=" "%MS_LOG%.state"') do set "POLD=%%a"
  for /f "tokens=2 delims==" %%a in ('findstr /b "STALE=" "%MS_LOG%.state"') do set "PSTALE=%%a"
)
if not "%PSTATUS%"=="OURS" (
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$c=Get-NetTCPConnection -LocalPort %MS_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if($c){ Write-Host ('PORT %MS_PORT% is occupied by PID ' + $c.OwningProcess + '; not terminated') } else { Write-Host 'PORT %MS_PORT% is not listening' }; exit 0"
  echo PORT %MS_PORT% IS USED BY A FOREIGN PROCESS OR A DEAD CORE; not terminated.
  netstat -ano | findstr :%MS_PORT%
  exit /b 1
)

if "%PSTALE%"=="0" goto :open_existing
if /I "%START_CORE_NO_RESTART%"=="1" (
  echo CODE UPDATED: restart skipped because START_CORE_NO_RESTART=1
  goto :open_existing
)

echo CODE UPDATED: restarting core
taskkill /PID %POLD% /T /F >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "for($i=0;$i -lt 40;$i++){ if(-not (Get-NetTCPConnection -LocalPort %MS_PORT% -State Listen -ErrorAction SilentlyContinue)){exit 0}; Start-Sleep -Milliseconds 250 }; exit 1"
if errorlevel 1 (
  echo OLD CORE DID NOT STOP. Close it manually and retry.
  pause
  exit /b 1
)
goto :start

:open_existing
if /I "%START_CORE_NO_BROWSER%"=="1" (
  echo HEALTH_OK
  echo core already running [pid=%POLD%, code=latest]
  exit /b 0
)
start "" "http://127.0.0.1:%MS_PORT%/"
echo core already running [pid=%POLD%, code=latest]; browser opened
exit /b 0

:start
if exist "%MS_LOG%.pid" del "%MS_LOG%.pid" >nul 2>&1
start "ModelSmith Core" /MIN cmd /c ""%MS_PY%" "%MS_CORE%" --port %MS_PORT% --data-dir "%MS_DATA%" --laya-python "%MS_PY%" --llm-python "%MS_PY%" --aitk-python "%MS_PY%" --diffsynth-python "%MS_PY%" --lf-python "%MS_PY%" > "%MS_LOG%" 2>&1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$ok=$false; for($i=0;$i -lt 60;$i++){ try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:%MS_PORT%/health' -TimeoutSec 1; if($h.ok -and $h.pid){ Set-Content -Path '%MS_LOG%.pid' -Value $h.pid -Encoding ascii; $ok=$true; break } } catch {}; if($i %% 10 -eq 9){ Write-Host 'waiting for core health...' }; Start-Sleep -Milliseconds 500 }; if($ok){exit 0}else{exit 1}"
if errorlevel 1 goto :fail
set "NEWPID="
if exist "%MS_LOG%.pid" set /p NEWPID=<"%MS_LOG%.pid"
echo core started (pid=%NEWPID%, code=latest)

if /I "%START_CORE_NO_BROWSER%"=="1" (
  echo HEALTH_OK
  if /I "%START_CORE_KEEP%"=="1" exit /b 0
  call :stopcore
  exit /b 0
)
start "" "http://127.0.0.1:%MS_PORT%/"
echo ModelSmith core is running at http://127.0.0.1:%MS_PORT%/
exit /b 0

:fail
powershell -NoProfile -ExecutionPolicy Bypass -Command "$h=$null; try { $h=Invoke-RestMethod -Uri 'http://127.0.0.1:%MS_PORT%/health' -TimeoutSec 2 } catch {}; if($h -and $h.ok -and $h.pid){ Set-Content -Path '%MS_LOG%.pid' -Value $h.pid -Encoding ascii; exit 0 }; $c=$null; try { $c=Get-Content -Path '%MS_DATA%\core.json' -Raw | ConvertFrom-Json } catch {}; if($c -and [int]$c.port -eq %MS_PORT% -and $c.pid -and (Get-Process -Id ([int]$c.pid) -ErrorAction SilentlyContinue)){ exit 0 }; exit 1"
if errorlevel 1 goto :fail_stop
echo CORE IS ALIVE BUT NOT HEALTHY YET; leaving it untouched.
exit /b 0
:fail_stop
echo CORE DID NOT BECOME HEALTHY IN 30s.
echo Log: %MS_LOG%
echo ---- launcher.log (tail) ----
powershell -NoProfile -ExecutionPolicy Bypass -Command "if (Test-Path '%MS_LOG%') { Get-Content -Path '%MS_LOG%' -Tail 20 }"
call :stopcore
exit /b 1

:stopcore
set "OWNPID="
if exist "%MS_LOG%.pid" set /p OWNPID=<"%MS_LOG%.pid"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$own='%OWNPID%'.Trim(); $listener=Get-NetTCPConnection -LocalPort %MS_PORT% -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if(-not $listener){ exit 0 }; $ownerPid=[int]$listener.OwningProcess; if($own -and $ownerPid -eq [int]$own){ Stop-Process -Id $ownerPid -Force; exit 0 }; Write-Host ('PORT %MS_PORT% is occupied by PID ' + $ownerPid + '; not terminated'); exit 2"
exit /b 0
