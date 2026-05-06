#!/bin/bash
set -e

echo "========================================="
echo " Anonymizer — сборка macOS (.app / Unix)"
echo "========================================="

# Устанавливаем зависимости
pip3 install -r requirements.txt
pip3 install pyinstaller

# Сборка
pyinstaller \
  --onefile \
  --windowed \
  --name Anonymizer \
  --add-data "static:static" \
  --add-data "core:core" \
  --hidden-import=natasha \
  --hidden-import=pymorphy2 \
  --hidden-import=pymorphy2.tagset \
  --hidden-import=pymorphy2.analyzer \
  --hidden-import=docx \
  --hidden-import=fitz \
  --hidden-import=pdfplumber \
  --hidden-import=pdfminer \
  --hidden-import=pdfminer.high_level \
  --hidden-import=openpyxl \
  --hidden-import=striprtf \
  --hidden-import=striprtf.striprtf \
  --hidden-import=flask \
  --hidden-import=flask_cors \
  --hidden-import=werkzeug \
  --hidden-import=jinja2 \
  --hidden-import=click \
  --collect-all natasha \
  --collect-all pymorphy2 \
  --collect-all navec \
  --collect-all razdel \
  app.py

echo ""
echo "========================================="
echo " Готово!"
echo " dist/Anonymizer   — однофайловый бинарник"
echo " (или dist/Anonymizer.app — если --windowed создал .app)"
echo ""
echo " Для запуска без сборки:"
echo "   python3 app.py"
echo "========================================="
