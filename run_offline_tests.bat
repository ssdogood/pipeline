@echo off
rem =====================================================================
rem  run_offline_tests.bat  -  the GREEN SCRIPT, Windows edition.
rem
rem  Native cmd.exe port of run_offline_tests.sh. NO Git-Bash, NO MSYS,
rem  NO PowerShell required. Verification uses certutil (ships with
rem  every Windows) for SHA-256 checksums.
rem
rem  Does the same job as the Unix green script:
rem    Step 1: checksum-verify the pinned WINDOWS binaries in bin\
rem            against bin\sops_manifest.txt  ("verify, then trust")
rem    Step 2: run the full offline test suite (pytest tests\) using a
rem            Python that has PyYAML (the conda 'dpl' env, if active)
rem
rem  Usage:    run_offline_tests.bat        (from a cmd.exe prompt in
rem                                          the project root)
rem  Success:  "GREEN" banner, exit code 0.
rem  Failure:  "RED" message naming what to fix, exit code 1.
rem =====================================================================

setlocal EnableExtensions EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "BIN_DIR=%SCRIPT_DIR%\bin"
set "MANIFEST=%BIN_DIR%\sops_manifest.txt"
set /a VERIFIED=0
set /a BAD=0
set /a SKIPPED=0

echo === pass_mgr green script - machine certification (Windows) ===
echo.

echo [1/3] Platform: Windows (cmd.exe)
echo.

rem -------------------------------------------------------------------
rem  Step 1: checksum-verify the pinned Windows binaries
rem -------------------------------------------------------------------

if not exist "%MANIFEST%" goto no_manifest

echo [2/3] Verifying pinned Windows binaries against sops_manifest.txt:

rem  Manifest format is the shasum format:  <sha256><2 spaces><filename>
rem  We keep only the lines for Windows binaries (matching amd64.exe),
rem  skip comment lines (starting with '#'), then verify each one.

for /f "usebackq eol=# tokens=1,* delims= " %%a in (`findstr /i /c:"amd64.exe" "%MANIFEST%"`) do (
    set "WANT=%%a"
    set "FNAME=%%b"
    call :verify_one
)

if %BAD% GTR 0 goto checksum_failed
if %VERIFIED% EQU 0 if %SKIPPED% EQU 0 goto no_entries
if %VERIFIED% GTR 0 (
    echo       all present Windows binaries verified OK
) else (
    echo       no SOPS/Age binaries present - FERNET-ONLY mode
)
echo.

rem -------------------------------------------------------------------
rem  Step 2: find a Python with PyYAML, then run the offline suite
rem -------------------------------------------------------------------

set "PY="
set "PYARGS="

rem  Prefer the active conda environment (matches vault.sh behavior).
if defined CONDA_PREFIX (
    if exist "%CONDA_PREFIX%\python.exe" (
        "%CONDA_PREFIX%\python.exe" -c "import yaml" >nul 2>&1
        if not errorlevel 1 set "PY=%CONDA_PREFIX%\python.exe"
    )
)

rem  Otherwise: any python.exe on PATH that has PyYAML.
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY (
            "%%p" -c "import yaml" >nul 2>&1
            if not errorlevel 1 set "PY=%%p"
        )
    )
)

rem  Last resort: the Windows py launcher.
if not defined PY (
    py -3 -c "import yaml" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py"
        set "PYARGS=-3"
    )
)

if not defined PY goto no_python

cd /d "%SCRIPT_DIR%"
echo [3/3] Running offline test suite:
"%PY%" %PYARGS% -m pytest tests\ -v
if errorlevel 1 goto tests_red

rem -------------------------------------------------------------------
rem  Certified
rem -------------------------------------------------------------------

if %VERIFIED% GTR 0 goto green_full
echo.
echo ==================================================
echo  GREEN - tree certified (FERNET-ONLY):
echo    no SOPS/Age binaries present; the binary-
echo    dependent tests are SKIPPED (listed in the
echo    summary above - coverage is reduced, not
echo    hidden). Fernet vault, config, common, and
echo    writer suites fully passing.
echo  Download SOPS/Age per bin\sops_manifest.txt
echo  to certify at full strength.
echo ==================================================
exit /b 0

:green_full
echo.
echo ==================================================
echo  GREEN - this machine's tree is certified:
echo    Windows binaries checksum-verified, full offline
echo    suite passing, zero network used.
echo  Trust this copy for extraction work.
echo ==================================================
exit /b 0

rem -------------------------------------------------------------------
rem  One-binary verification subroutine (called by the loop above)
rem -------------------------------------------------------------------

:verify_one
if "!FNAME!"=="" goto :eof
if not exist "%BIN_DIR%\!FNAME!" (
    echo       SKIPPED : !FNAME! - not present, optional
    set /a SKIPPED+=1
    goto :eof
)
set "HAVE="
for /f "skip=1 delims=" %%h in ('certutil -hashfile "%BIN_DIR%\!FNAME!" SHA256') do (
    if not defined HAVE set "HAVE=%%h"
)
if not defined HAVE (
    echo       ERROR   : could not hash !FNAME!
    set /a BAD+=1
    goto :eof
)
rem  certutil pads the hash with spaces in some locales; strip them.
set "HAVE=!HAVE: =!"
set "WANT=!WANT: =!"
rem  certutil prints UPPERCASE, manifests are usually lowercase; /i = ignore case.
if /i "!HAVE!"=="!WANT!" (
    echo       OK      : !FNAME!
    set /a VERIFIED+=1
) else (
    echo       MISMATCH: !FNAME!
    echo                manifest : !WANT!
    echo                computed : !HAVE!
    set /a BAD+=1
)
goto :eof

rem -------------------------------------------------------------------
rem  Failure exits
rem -------------------------------------------------------------------

:no_manifest
echo.
echo RED - certification FAILED: bin\sops_manifest.txt not found.
echo The manifest is committed in git; your tree is incomplete.
exit /b 1

:no_entries
echo.
echo RED - certification FAILED: no manifest entries matched Windows binaries.
echo sops_manifest.txt has no lines for this platform (looked for: amd64.exe).
echo Copy the pinned Windows binaries into bin\ and add their lines to the
echo manifest, then re-run run_offline_tests.bat
exit /b 1

:checksum_failed
echo.
echo RED - certification FAILED: checksum verification failed.
echo The file(s) above do not match the pinned SHA-256 in the manifest.
echo Replace them from the official release, then re-run.
exit /b 1

:no_python
echo.
echo RED - certification FAILED: no Python found with PyYAML installed.
echo Fix:  conda activate dpl        (recommended)
echo    or: pip install pyyaml
echo Then re-run run_offline_tests.bat
exit /b 1

:tests_red
echo.
echo RED - certification FAILED: test suite is RED (see failures above).
exit /b 1
