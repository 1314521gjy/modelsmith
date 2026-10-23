@echo off
setlocal
set "MS_ROOT=%~dp0"
set "MS_ROOT=%MS_ROOT:~0,-1%"
set "MS_DATA=C:\ModelSmith\state"
if defined MODELSMITH_RUNTIME set "MS_RUNTIME=%MODELSMITH_RUNTIME%"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=C:\ModelSmith\runtime"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=%MS_ROOT%\runtime"
if not exist "%MS_RUNTIME%\python\python.exe" set "MS_RUNTIME=%MS_ROOT%\..\runtime"
if not exist "%MS_RUNTIME%\python\python.exe" (
  echo RUNTIME MISSING. Looked for: MODELSMITH_RUNTIME, C:\ModelSmith\runtime, dist\runtime, runtime
  exit /b 1
)
rem Do NOT set PYTHONHOME/PYTHONPATH here: the embedded runtime finds its own
rem stdlib/site-packages via python312._pth, and inheriting PYTHONHOME into every
rem child process (adapters, other pythons, test stubs) breaks them
rem ("ModuleNotFoundError: No module named 'encodings'").
set "PYTHONUTF8=1"
set "PYTHONNOUSERSITE=1"
set "MS_PY=%MS_RUNTIME%\python\python.exe"
set "MS_LAUNCHER_VERSION=V6"
rem V6: no 5s health timeout and no blind port kill here; this launcher runs the core in the foreground.
rem Port ownership checks / restart safety live in start-modelsmith.cmd.
set "MS_CORE=%MS_ROOT%\core\modelsmith_core.py"
if not exist "%MS_CORE%" set "MS_CORE=%MS_ROOT%\..\core\modelsmith_core.py"
"%MS_PY%" "%MS_CORE%" --data-dir "%MS_DATA%" --laya-python "%MS_PY%" --llm-python "%MS_PY%" --aitk-python "%MS_PY%" --diffsynth-python "%MS_PY%" --lf-python "%MS_PY%" %*
exit /b %ERRORLEVEL%
