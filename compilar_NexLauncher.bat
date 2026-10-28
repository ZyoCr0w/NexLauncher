@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
title Compilar NexLauncher.py - PyInstaller

echo ============================================
echo   Compilador PyInstaller - NexLauncher.py
echo ============================================

where python >nul 2>&1
if errorlevel 1 (
    echo Python no esta instalado. Intentando instalar con winget...
    where winget >nul 2>&1
    if errorlevel 1 (
        echo [ERROR] winget no disponible en este Windows.
        echo Instala Python manualmente desde https://www.python.org/downloads/ y vuelve a correr este script.
        pause
        exit /b 1
    )
    winget install -e --id Python.Python.3.12 --silent
    echo Python se instalo. Cierra esta ventana y vuelve a hacer doble clic en este script
    echo para que la terminal cargue el PATH actualizado.
    pause
    exit /b 0
)

echo.
echo [1/3] Verificando pip...
python -m pip install --upgrade pip >nul 2>&1

echo [2/3] Verificando/instalando dependencias...
python -m pip show pyinstaller >nul 2>&1 || python -m pip install pyinstaller==6.22.3
python -m pip show pillow >nul 2>&1 || python -m pip install pillow==13.0.0

echo Buscando recursos graficos en esta carpeta (iconos/imagenes)...
set RES_ARGS=
for %%f in (*.ico *.png *.jpg *.jpeg *.gif *.webp) do (
    if exist "%%f" set RES_ARGS=!RES_ARGS! --add-data "%%f;."
)
if exist "client_id.txt" (
    echo Client ID de Azure encontrado: se integra en el .exe.
    set RES_ARGS=!RES_ARGS! --add-data "client_id.txt;."
) else (
    echo [AVISO] No hay client_id.txt: el login con Microsoft pedira el Client ID la primera vez.
)

echo Buscando icono para el ejecutable...
set ICON_ARG=
set ICON_SRC=
if exist "icono.ico" set "ICON_SRC=icono.ico"
if not defined ICON_SRC if exist "icono.ico" set "ICON_SRC=icono.ico"
if not defined ICON_SRC if exist "icono.png" set "ICON_SRC=icono.png"
if not defined ICON_SRC if exist "icono.jpg" set "ICON_SRC=icono.jpg"
if defined ICON_SRC (
    if /i "!ICON_SRC:~-4!"==".ico" (
        set "ICON_ARG=--icon=!ICON_SRC!"
    ) else (
        python -m pip show pillow >nul 2>&1 || python -m pip install pillow >nul 2>&1
        python -c "from PIL import Image; Image.open(r'!ICON_SRC!').convert('RGBA').save('_autoicon.ico', sizes=[(16,16),(32,32),(48,48),(256,256)])" >nul 2>&1 && set "ICON_ARG=--icon=_autoicon.ico"
    )
)
if defined ICON_SRC if not defined ICON_ARG (
    echo [AVISO] Se encontro "!ICON_SRC!" pero no se pudo convertir a .ico. El ejecutable saldra sin icono personalizado.
)

echo [3/3] Compilando con PyInstaller...
python -m PyInstaller --onefile --windowed --name NexLauncher !RES_ARGS! !ICON_ARG! "NexLauncher.py"

if errorlevel 1 (
    echo.
    echo [ERROR] La compilacion fallo. Revisa el mensaje de arriba.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Listo. Ejecutable en: dist\NexLauncher.exe
echo ============================================
pause
