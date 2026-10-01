@echo off
chcp 65001 >nul
rem Сборка Anonymizer для Windows: build_windows.bat  (Python 3.12)
cd /d "%~dp0"
if not exist .venv312 py -3.12 -m venv .venv312
.venv312\Scripts\pip install -q --upgrade pip
.venv312\Scripts\pip install -q -r requirements-dev.txt
.venv312\Scripts\python -c "from PIL import Image; Image.open('assets/icon_1024.png').save('assets/Anonymizer.ico', sizes=[(16,16),(32,32),(48,48),(64,64),(128,128),(256,256)])"
.venv312\Scripts\pyinstaller --noconfirm --clean Anonymizer.spec
echo.
echo Готово: dist\Anonymizer\Anonymizer.exe
echo Распознавание сканов и фото на Windows недоступно (только macOS).
pause
