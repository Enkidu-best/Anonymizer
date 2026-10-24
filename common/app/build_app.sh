#!/bin/bash
# Сборка .app одним способом для всех программ (стандарт 6.2; эталон - Interview Coach desktop/build_app.sh).
# Исполняемый файл - launcher.c (clang, arm64), логика - в launcher.sh программы, подпись ad-hoc, регистрация в системе.
#
#   build_app.sh --name "Видеокарта" --id local.andrey.gpu-page --dest <папка для .app> --script <launcher.sh> \
#                [--icon <значок .icns или .png>] [--version 1.3.0] [--ui-element] [--applications]
#
# --script внутри --dest записывается относительным путём (папку можно перенести), иначе - абсолютным.
# --ui-element - без значка в Dock (служебная программа). --applications - ссылка в /Applications.
# Пересборка меняет подпись: macOS снова спросит доступ к папкам и микрофону - собирать один раз.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="" ID="" DEST="" SCRIPT="" ICON="" VER="1.0" UI=0 APPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2 ;;
    --id) ID="$2"; shift 2 ;;
    --dest) DEST="$2"; shift 2 ;;
    --script) SCRIPT="$2"; shift 2 ;;
    --icon) ICON="$2"; shift 2 ;;
    --version) VER="$2"; shift 2 ;;
    --ui-element) UI=1; shift ;;
    --applications) APPS=1; shift ;;
    *) echo "неизвестный параметр: $1"; exit 2 ;;
  esac
done
[ -n "$NAME" ] && [ -n "$ID" ] && [ -n "$DEST" ] && [ -n "$SCRIPT" ] || { sed -n '2,10p' "$0"; exit 2; }
DEST="$(cd "$DEST" && pwd)"
SCRIPT="$(cd "$(dirname "$SCRIPT")" && pwd)/$(basename "$SCRIPT")"
[ -f "$SCRIPT" ] || { echo "нет скрипта: $SCRIPT"; exit 2; }
case "$SCRIPT" in "$DEST"/*) REL="${SCRIPT#"$DEST"/}" ;; *) REL="$SCRIPT" ;; esac
APP="$DEST/$NAME.app"

if [ -d "$APP" ]; then                     # прежняя сборка - в корзину (~/.Trash), не безвозвратно
  mv "$APP" "$HOME/.Trash/$NAME (прежняя сборка $(date +%d.%m\ %H-%M-%S)).app"
fi
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
clang -O2 -arch arm64 -DAPP_NAME="\"$NAME\"" -DSCRIPT_REL="\"$REL\"" -o "$APP/Contents/MacOS/launcher" "$HERE/launcher.c"
chmod +x "$APP/Contents/MacOS/launcher"
ICONKEY=""
if [ -n "$ICON" ]; then
  case "$ICON" in
    *.icns) cp "$ICON" "$APP/Contents/Resources/AppIcon.icns" ;;
    *.png)
      SET="${TMPDIR:-/tmp}/appicon.$$.iconset"; mkdir -p "$SET"
      for s in 16 32 128 256 512; do
        sips -z $s $s "$ICON" --out "$SET/icon_${s}x${s}.png" >/dev/null
        sips -z $((s * 2)) $((s * 2)) "$ICON" --out "$SET/icon_${s}x${s}@2x.png" >/dev/null
      done
      iconutil -c icns "$SET" -o "$APP/Contents/Resources/AppIcon.icns"; rm -rf "$SET" ;;
  esac
  ICONKEY="<key>CFBundleIconFile</key><string>AppIcon</string>"
fi
UIKEY=""; [ $UI = 1 ] && UIKEY="<key>LSUIElement</key><true/>"
cat >"$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>$NAME</string>
    <key>CFBundleDisplayName</key><string>$NAME</string>
    <key>CFBundleIdentifier</key><string>$ID</string>
    <key>CFBundleVersion</key><string>$VER</string>
    <key>CFBundleShortVersionString</key><string>$VER</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleExecutable</key><string>launcher</string>
    $ICONKEY
    $UIKEY
    <key>NSHighResolutionCapable</key><true/>
    <key>LSArchitecturePriority</key><array><string>arm64</string></array>
    <key>LSRequiresNativeExecution</key><true/>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>NSDesktopFolderUsageDescription</key><string>Программа лежит в папке на рабочем столе: нужен доступ, чтобы её запустить.</string>
</dict>
</plist>
PLIST
plutil -lint "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - --identifier "$ID" "$APP"
if [ $APPS = 1 ]; then rm -f "/Applications/$NAME.app"; ln -s "$APP" "/Applications/$NAME.app"; fi
LSREG=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
"$LSREG" -f "$APP" >/dev/null 2>&1 || true
touch "$APP"
echo "Собрано: $APP (скрипт: $REL)"
