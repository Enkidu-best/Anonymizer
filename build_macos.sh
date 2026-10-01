#!/bin/bash
set -e

echo "========================================="
echo " Anonymizer — сборка macOS (.app / Unix)"
echo "========================================="

# Устанавливаем зависимости
pip3 install -r requirements-dev.txt

# Сборка
pyinstaller \
  --onefile \
  --windowed \
  --name Anonymizer \
  --add-data "static:static" \
  --add-data "core:core" \
  --hidden-import=docx \
  --hidden-import=fitz \
  --hidden-import=openpyxl \
  --hidden-import=striprtf \
  --hidden-import=striprtf.striprtf \
  --hidden-import=flask \
  --hidden-import=werkzeug \
  --hidden-import=jinja2 \
  --hidden-import=click \
  --collect-all spacy \
  --collect-all ru_core_news_lg \
  --collect-all thinc \
  --collect-all pymorphy3 \
  --collect-all pymorphy3_dicts_ru \
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
