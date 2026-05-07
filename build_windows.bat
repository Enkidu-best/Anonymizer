@echo off
echo =========================================
echo  Anonymizer - Windows build (.exe)
echo =========================================
echo.
echo NOTE: Build requires Python 3.11 or 3.12 for best compatibility.
echo       numpy 1.x does not support Python 3.13+.
echo       If NER fails in the .exe, rebuild with Python 3.11/3.12.
echo.

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
  --hidden-import=pymorphy2_dicts ^
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
  --hidden-import=pdf2docx ^
  --collect-all natasha ^
  --collect-all pymorphy2 ^
  --collect-all pymorphy2_dicts ^
  --collect-all navec ^
  --collect-all razdel ^
  --collect-all pdf2docx ^
  app.py

echo.
echo =========================================
echo  Done!  dist\Anonymizer.exe
echo  Copy the file anywhere and run it.
echo  Browser will open automatically.
echo  Natasha models download once (~220 MB).
echo =========================================
pause
