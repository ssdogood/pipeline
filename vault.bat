@echo off
rem =====================================================================
rem  vault.bat  -  in-memory vault operations for pass_mgr
rem                 (Windows native edition of vault.sh)
rem
rem  Usage:
rem    vault list  <env>               print secret names (never values)
rem    vault scan  <env> [--dry-run]    sync vaults to match config files
rem
rem  Policies enforced (inherited from scripts\vault_ops.py):
rem    - decrypted plaintext exists only in memory, never written to disk
rem    - values are never echoed, printed, or logged
rem    - atomic replacement of the vault file
rem    - verification round-trip before the old vault is replaced
rem =====================================================================

setlocal EnableExtensions

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "OPS_PY=%SCRIPT_DIR%\scripts\vault_ops.py"

rem -------------------------------------------------------------------
rem  Find a Python that has PyYAML. Same preference order as the other
rem  Windows scripts and as vault.sh: active conda env first, then any
rem  python on PATH, then the py launcher. Probing with 'import yaml'
rem  guarantees the interpreter that runs the vault can parse configs.
rem -------------------------------------------------------------------

set "PY="
set "PYARGS="

rem  1) Active conda environment (e.g. conda activate dpl).
if defined CONDA_PREFIX (
    if exist "%CONDA_PREFIX%\python.exe" (
        "%CONDA_PREFIX%\python.exe" -c "import yaml" >nul 2>&1
        if not errorlevel 1 set "PY=%CONDA_PREFIX%\python.exe"
    )
)

rem  2) Any python.exe on PATH that has PyYAML.
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY (
            "%%p" -c "import yaml" >nul 2>&1
            if not errorlevel 1 set "PY=%%p"
        )
    )
)

rem  3) The Windows py launcher (Python 3).
if not defined PY (
    py -3 -c "import yaml" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py"
        set "PYARGS=-3"
    )
)

if not defined PY goto no_python

if not exist "%OPS_PY%" goto no_script

rem  Hand over with ALL arguments unchanged (vault_ops.py owns parsing,
rem  usage text, and exit codes). 'cmd /c wait' is implicit in a bat:
rem  the child's exit code flows to the caller automatically because it
rem  is the last command executed here.
"%PY%" %PYARGS% "%OPS_PY%" %*
exit /b %errorlevel%

rem -------------------------------------------------------------------
rem  Failure exits
rem -------------------------------------------------------------------

:no_python
echo.
echo VAULT ERROR: no Python found with PyYAML installed.
echo Fix:  conda activate dpl        ^(recommended^)
echo    or: pip install pyyaml
echo Then re-run this command.
echo.
exit /b 1

:no_script
echo.
echo VAULT ERROR: scripts\vault_ops.py not found at:
echo   %OPS_PY%
echo Your tree is incomplete - restore the scripts\ directory from git.
echo.
exit /b 1
