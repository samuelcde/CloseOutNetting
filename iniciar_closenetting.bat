@echo off
setlocal

cd /d "%~dp0"

title SET ICAP - Auto CloseNetting

chcp 65001 >nul

cls

echo ===============================================================
echo              SET ICAP - AUTO CLOSENETTING
echo ===============================================================
echo.
echo Directorio:
echo %CD%
echo.
echo Fecha de inicio:
echo %DATE% %TIME%
echo.
echo ===============================================================
echo.

if not exist "venv\Scripts\activate.bat" (
    echo [ERROR] No se encontro el entorno virtual.
    echo.
    pause
    exit /b 1
)

call "venv\Scripts\activate.bat"

echo Entorno virtual activado.
echo.
echo ===============================================================
echo              INICIANDO MONITOR DE CORREOS
echo ===============================================================
echo.

:START

python -u main.py

set EXIT_CODE=%ERRORLEVEL%

echo.
echo ===============================================================
echo El proceso Python finalizo.
echo Codigo de salida: %EXIT_CODE%
echo Fecha: %DATE% %TIME%
echo ===============================================================
echo.

if "%EXIT_CODE%"=="0" goto END

echo Se intentara reiniciar el proceso en 30 segundos...
echo Presione CTRL+C para cancelar.
echo.

timeout /t 30 /nobreak >nul

goto START


:END
echo.
echo El monitor fue detenido normalmente.
echo.

pause

endlocal