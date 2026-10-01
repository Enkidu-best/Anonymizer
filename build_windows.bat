@echo off
echo =========================================
echo  Anonymizer - Windows build (.exe)
echo =========================================
echo.
echo NOTE: Build requires Python 3.11 or 3.12 for best compatibility.
echo       numpy 1.x does not support Python 3.13+.
echo       If NER fails in the .exe, rebuild with Python 3.11/3.12.
echo.

pip install -r requirements-dev.txt

python -m PyInstaller ^
  --onefile ^
  --windowed ^
  --name Anonymizer ^
  --add-data "static;static" ^
  --add-data "core;core" ^
  --hidden-import=docx ^
  --hidden-import=fitz ^
  --hidden-import=openpyxl ^
  --hidden-import=striprtf ^
  --hidden-import=striprtf.striprtf ^
  --hidden-import=flask ^
  --hidden-import=werkzeug ^
  --hidden-import=jinja2 ^
  --hidden-import=click ^
  --collect-all spacy ^
  --collect-all ru_core_news_lg ^
  --collect-all thinc ^
  --collect-all pymorphy3 ^
  --collect-all pymorphy3_dicts_ru ^
  app.py

echo.
echo =========================================
echo  Done!  dist\Anonymizer.exe
echo  Copy the file anywhere and run it.
echo  Browser will open automatically.
echo  spaCy model is bundled inside the .exe.
echo =========================================
pause
