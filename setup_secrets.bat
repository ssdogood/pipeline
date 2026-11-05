@echo off
rem =====================================================================
rem  setup_secrets.bat  -  create age identity and Fernet key
rem                         (Windows native edition of setup_secrets.sh)
rem
rem  Usage:   setup_secrets.bat laptop    (or prod)
rem
rem  Creates keys under %USERPROFILE%\.dpl-keys\<env>\, never overwrites
rem  existing keys, tightens NTFS permissions with icacls (the real
rem  successor of chmod on Windows), and prints the config values for
rem  pass_mgr.<env>.yaml.
rem
rem  IMPORTANT - the config contract: key-file paths in the YAML config
rem  MUST be written in tilde form (~/.dpl-keys/...), never as absolute
rem  Windows paths. resolve_relative_path() accepts ONLY tilde-prefixed
rem  paths for key files. The report below prints the exact values to
rem  paste, in the correct form already.
rem =====================================================================

setlocal EnableExtensions EnableDelayedExpansion

rem --- Step 1: argument ----------------------------------------------------
set "ENV=%~1"
if "%ENV%"=="" goto usage
if /i not "%ENV%"=="laptop" if /i not "%ENV%"=="prod" goto bad_env

set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "BIN_DIR=%SCRIPT_DIR%\bin"

rem --- Step 2: key paths (defaults, or custom with confirmation) -----------
set "HOME_KEYROOT=%USERPROFILE%\.dpl-keys"
set "DEFAULT_AGE=%HOME_KEYROOT%\%ENV%\sops-age\age.key"
set "DEFAULT_FERNET=%HOME_KEYROOT%\%ENV%\fernet\key"

echo.
echo === pass_mgr key setup - environment: %ENV% ===
echo.
echo Default key locations:
echo   Age key:    %DEFAULT_AGE%
echo   Fernet key: %DEFAULT_FERNET%
echo.
set "ACCEPT_DEFAULTS="
set /p ACCEPT_DEFAULTS="Accept defaults? (y/n) "

if /i "%ACCEPT_DEFAULTS%"=="y" goto defaults_set

echo.
set "AGE_PATH="
set /p AGE_PATH="Age key path [Enter = default]: "
if not defined AGE_PATH set "AGE_PATH=%DEFAULT_AGE%"
set "FERNET_PATH="
set /p FERNET_PATH="Fernet key path [Enter = default]: "
if not defined FERNET_PATH set "FERNET_PATH=%DEFAULT_FERNET%"
echo.
echo Using custom locations:
echo   Age key:    %AGE_PATH%
echo   Fernet key: %FERNET_PATH%
set "CONT="
set /p CONT="Continue? (y/n) "
if /i not "%CONT%"=="y" (
    echo.
    echo Setup aborted.
    exit /b 0
)
goto step3

:defaults_set
set "AGE_PATH=%DEFAULT_AGE%"
set "FERNET_PATH=%DEFAULT_FERNET%"

:step3
rem --- Step 3: locate the WINDOWS age-keygen binary ------------------------
rem  Windows builds carry the .exe suffix (amd64.exe), per sops_manifest.txt.
rem  Picking by platform is mandatory: a tree holding several platforms'
rem  binaries must not hand this script a darwin/ELF build.
set "KGEN_BIN="
for %%f in ("%BIN_DIR%\age-keygen-*amd64.exe") do if not defined KGEN_BIN set "KGEN_BIN=%%~f"
if not defined KGEN_BIN goto no_kgen

rem --- Step 4: create directories and lock permissions ---------------------
if not exist "%HOME_KEYROOT%" mkdir "%HOME_KEYROOT%"
call :lock_dir "%HOME_KEYROOT%"

for %%d in ("%AGE_PATH%" "%FERNET_PATH%") do (
    if not exist "%%~dpd" mkdir "%%~dpd"
    call :lock_dir "%%~dpd"
)

rem --- Step 5: mint age identity (refuse overwrite) -------------------------
if exist "%AGE_PATH%" (
    echo.
    echo Age key already exists at %AGE_PATH% - skipping ^(protected^).
    call :show_pubkey
) else (
    echo.
    echo Minting age identity...
    "%KGEN_BIN%" -o "%AGE_PATH%" > "%TEMP%\pm_agekeygen.out" 2>&1
    if errorlevel 1 goto kgen_failed
    type "%TEMP%\pm_agekeygen.out"
    del "%TEMP%\pm_agekeygen.out" >nul 2>&1
)

rem --- Step 6: generate Fernet key (refuse overwrite) -----------------------
if exist "%FERNET_PATH%" (
    echo.
    echo Fernet key already exists at %FERNET_PATH% - skipping ^(protected^).
) else (
    echo Generating Fernet key...
    call :find_python
    if not defined PY goto no_python
    rem  Pass the path via an environment variable - avoids quoting traps
    rem  in the -c snippet. Uses the SAME cryptography package the tests use.
    set "FPL=%FERNET_PATH%"
    "!PY!" !PYARGS! -c "import os; from cryptography.fernet import Fernet; open(os.environ['FPL'],'wb').write(Fernet.generate_key())"
    if errorlevel 1 goto fernet_failed
)

rem --- Step 7: lock key file permissions + report ---------------------------
call :lock_file "%AGE_PATH%"
call :lock_file "%FERNET_PATH%"

echo.
echo ==============================================
echo   Setup complete for '%ENV%'.
echo.
echo   Put THESE values in config\pass_mgr.%ENV%.yaml
echo   ^(copy exactly - tilde form, forward slashes^):
echo.
echo     age_key_file: ~/.dpl-keys/%ENV%/sops-age/age.key
echo     key_file:     ~/.dpl-keys/%ENV%/fernet/key
echo.
echo   Then run: run_offline_tests.bat
echo ==============================================
echo.
exit /b 0

rem ---------------------------------------------------------------------
rem  Subroutines
rem ---------------------------------------------------------------------

:show_pubkey
rem  Print the age1... public key from the identity file.
set "PK="
for /f "tokens=*" %%k in ('findstr /c:"age1" "%AGE_PATH%"') do set "PK=%%k"
if defined PK (
    set "PK=!PK:*age1=age1!"
    echo   Public key: !PK!
) else (
    echo   ^(no public key comment found inside %AGE_PATH%^)
)
goto :eof

:lock_dir
rem  NTFS equivalent of 'chmod 700' for a directory: disable inherited
rem  permissions, then grant full control to the current user only.
set "LK_TARGET=%~1"
if "!LK_TARGET:~-1!"=="\" set "LK_TARGET=!LK_TARGET:~0,-1!"
icacls "!LK_TARGET!" /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F" >nul 2>&1
if errorlevel 1 echo   ^(warning: could not tighten permissions on !LK_TARGET!^)
goto :eof

:lock_file
rem  NTFS equivalent of 'chmod 600' for a file.
set "LK_TARGET=%~1"
if "!LK_TARGET:~-1!"=="\" set "LK_TARGET=!LK_TARGET:~0,-1!"
icacls "!LK_TARGET!" /inheritance:r /grant:r "%USERNAME%:F" >nul 2>&1
if errorlevel 1 echo   ^(warning: could not tighten permissions on !LK_TARGET!^)
goto :eof

:find_python
rem  Prefer the active conda environment (matches vault.sh / the green
rem  script), then any Python on PATH with 'cryptography', then py -3.
set "PY="
set "PYARGS="
if defined CONDA_PREFIX (
    if exist "%CONDA_PREFIX%\python.exe" (
        "%CONDA_PREFIX%\python.exe" -c "import cryptography" >nul 2>&1
        if not errorlevel 1 set "PY=%CONDA_PREFIX%\python.exe"
    )
)
if not defined PY (
    for /f "delims=" %%p in ('where python 2^>nul') do (
        if not defined PY (
            "%%p" -c "import cryptography" >nul 2>&1
            if not errorlevel 1 set "PY=%%p"
        )
    )
)
if not defined PY (
    py -3 -c "import cryptography" >nul 2>&1
    if not errorlevel 1 (
        set "PY=py"
        set "PYARGS=-3"
    )
)
goto :eof

rem ---------------------------------------------------------------------
rem  Failure exits
rem ---------------------------------------------------------------------

:usage
echo.
echo SETUP FAILED: Usage: setup_secrets.bat ^<laptop^|prod^>
echo.
exit /b 1

:bad_env
echo.
echo SETUP FAILED: Environment must be 'laptop' or 'prod', got '%ENV%'.
echo.
exit /b 1

:no_kgen
echo.
echo SETUP FAILED: No Windows age-keygen binary found in bin\
echo ^(looked for: age-keygen-*amd64.exe^).
echo Copy the pinned Windows binary from the official release, keeping its
echo exact filename, add it to bin\sops_manifest.txt, then re-run
echo run_offline_tests.bat to verify.
echo.
exit /b 1

:kgen_failed
echo.
echo SETUP FAILED: age-keygen could not create the identity file.
echo See the error output above.
echo.
exit /b 1

:no_python
echo.
echo SETUP FAILED: no Python found with the 'cryptography' package.
echo Fix:   conda activate dpl        ^(recommended^)
echo     or: pip install cryptography
echo Then re-run setup_secrets.bat
echo.
exit /b 1

:fernet_failed
echo.
echo SETUP FAILED: Fernet key generation failed.
echo Activate the 'dpl' environment first:  conda activate dpl
echo.
exit /b 1
