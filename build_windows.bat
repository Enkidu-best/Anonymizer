@echo off
echo =========================================
echo  Anonymizer - Windows build (.exe)
echo =========================================

pip install -r requirements.txt
pip install pyinstaller

python -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name Anonymizer ^
  --add-data "static;static" ^
  --add-data "core;core" ^
  --hidden-import=natasha ^
  --hidden-import=pymorphy2 ^
  --hidden-import=pymorphy2.tagset ^
  --hidden-import=pymorphy2.analyzer ^
  --hidden-import=docx ^
  --hidden-import=fitz ^
  --hidden-import=pdfplumber ^
  --hidden-import=pdfminer ^
  --hidden-import=pdfminer.high_level ^
  --hidden-import=openpyxl ^
  --hidden-import=striprtf ^
  --hidden-import=striprtf.striprtf ^
  --hidden-import=flask ^
  --hidden-import=flask_cors ^
  --hidden-import=werkzeug ^
  --hidden-import=jinja2 ^
  --hidden-import=click ^
  --collect-all natasha ^
  --collect-all pymorphy2 ^
  --collect-all navec ^
  --collect-all razdel ^
  app.py

echo.
echo =========================================
echo  Done!  dist\Anonymizer.exe
echo  Copy the file anywhere and run it.
echo  Browser will open automatically.
echo  Natasha models download once (~220 MB).
echo =========================================
pause
