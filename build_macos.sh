#!/bin/bash
# Сборка Anonymizer.app и Anonymizer.dmg одной командой:  ./build_macos.sh
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12}
command -v "$PY" >/dev/null || PY=python3.12

echo "== 1/5 Окружение (.venv312)"
mkdir -p build
[ -d .venv312 ] || "$PY" -m venv .venv312
.venv312/bin/pip install -q --upgrade pip
if .venv312/bin/python -c "import ru_core_news_lg" 2>/dev/null; then
  # model already installed — do not download 500 MB again
  grep -v ru_core_news_lg requirements.txt > build/req-nomodel.txt
  .venv312/bin/pip install -q -r build/req-nomodel.txt pytest playwright pyinstaller
else
  .venv312/bin/pip install -q -r requirements-dev.txt
fi

echo "== 2/5 Иконка"
rm -rf build/icon.iconset && mkdir -p build/icon.iconset
for s in 16 32 128 256 512; do
  sips -z $s $s assets/icon_1024.png --out build/icon.iconset/icon_${s}x${s}.png >/dev/null
  sips -z $((s*2)) $((s*2)) assets/icon_1024.png --out build/icon.iconset/icon_${s}x${s}@2x.png >/dev/null
done
iconutil -c icns build/icon.iconset -o assets/Anonymizer.icns

echo "== 3/5 Сборка (PyInstaller, несколько минут)"
.venv312/bin/pyinstaller --noconfirm --clean Anonymizer.spec >build/pyinstaller.log 2>&1 || { tail -40 build/pyinstaller.log; exit 1; }

echo "== 4/5 Подпись (ad-hoc)"
codesign --force --deep -s - dist/Anonymizer.app

echo "== 5/5 DMG"
rm -rf build/dmg dist/Anonymizer.dmg && mkdir -p build/dmg
cp -R dist/Anonymizer.app build/dmg/
ln -s /Applications build/dmg/Applications
hdiutil create -volname Anonymizer -srcfolder build/dmg -ov -format UDZO dist/Anonymizer.dmg >/dev/null

echo ""
echo "Готово:"
echo "  dist/Anonymizer.app  — приложение"
echo "  dist/Anonymizer.dmg  — установщик (перетащить в «Программы»)"
du -sh dist/Anonymizer.app dist/Anonymizer.dmg
