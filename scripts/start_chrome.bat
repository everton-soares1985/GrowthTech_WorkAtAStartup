@echo off
setlocal
chcp 65001 >nul

REM =============================================================================
REM WORK AT A STARTUP CHROME MOTOR — perfil dedicado para login e automacao V0
REM Este launcher abre somente o navegador; nao lista nem envia candidaturas.
REM =============================================================================
set "CDP_HOST=127.0.0.1"
set "CDP_PORT=9226"
for %%I in ("%~dp0..") do set "PROJECT_ROOT=%%~fI"
set "PROFILE_DIR=%PROJECT_ROOT%\.chrome-profile"
set "INITIAL_URL=https://www.workatastartup.com/companies?demographic=any&hasEquity=any&hasSalary=any&industry=any&interviewProcess=any&layout=list-compact&sortBy=created_desc&tab=any&usVisaNotRequired=true"
set "CHROME_PATH=C:\Program Files\Google\Chrome\Application\chrome.exe"
set "BROWSER_MODE=VISIBLE"
set "HEADLESS_ARGS="

if /I "%~1"=="headless" (
    set "BROWSER_MODE=HEADLESS"
    set "HEADLESS_ARGS=--headless=new --disable-gpu"
) else if not "%~1"=="" (
    echo [BLOQUEADO] Modo desconhecido: %~1
    echo Use sem argumento para modo visual ou use: headless
    exit /b 1
)

echo ============================================================
echo   WORK AT A STARTUP - CHROME DEDICADO
echo ============================================================
echo   Host CDP : %CDP_HOST%
echo   Porta    : %CDP_PORT%
echo   Projeto  : %PROJECT_ROOT%
echo   Perfil   : %PROFILE_DIR%
echo   Modo     : %BROWSER_MODE%
echo.
echo   CHECKLIST MANUAL
echo   [ ] Nao ha outro Chrome usando a porta 9226
echo   [ ] O perfil pertence apenas ao Work at a Startup
echo   [ ] O login ja foi salvo previamente no modo visual
echo   [ ] Nao ha CAPTCHA ou challenge pendente
echo   [ ] Este launcher nao inicia candidaturas
echo.

powershell -NoProfile -Command "if (-not (Get-Command Get-NetTCPConnection -ErrorAction SilentlyContinue)) { exit 2 }; $listener = Get-NetTCPConnection -LocalPort %CDP_PORT% -State Listen -ErrorAction SilentlyContinue; if ($listener) { exit 1 } else { exit 0 }"
if errorlevel 2 (
    echo [BLOQUEADO] Nao foi possivel verificar a porta %CDP_PORT%.
    exit /b 1
)
if errorlevel 1 (
    echo [BLOQUEADO] A porta %CDP_PORT% ja esta em uso.
    exit /b 1
)

if not exist "%CHROME_PATH%" (
    echo [BLOQUEADO] Google Chrome nao encontrado.
    exit /b 1
)

if not exist "%PROFILE_DIR%" mkdir "%PROFILE_DIR%"
if not exist "%PROFILE_DIR%" (
    echo [BLOQUEADO] Nao foi possivel criar o perfil dedicado.
    exit /b 1
)

start "" "%CHROME_PATH%" %HEADLESS_ARGS% --remote-debugging-address=%CDP_HOST% --remote-debugging-port=%CDP_PORT% --user-data-dir="%PROFILE_DIR%" --new-window "%INITIAL_URL%"

echo Chrome dedicado iniciado em modo %BROWSER_MODE%.
if /I "%BROWSER_MODE%"=="VISIBLE" echo Faca login manualmente e mantenha a janela aberta.
echo Nenhuma automacao foi iniciada.
endlocal
