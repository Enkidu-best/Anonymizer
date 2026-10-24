#!/bin/bash
# Сборка Anonymizer.app общим сборщиком (стандарт 6.2, копия «_общие модули/app» - common/app):
# исполняемый файл - программа на C, логика - desktop/launcher.sh (правится без пересборки), подпись ad-hoc.
#   ./build_macos.sh       → Anonymizer.app в корне проекта; ярлык - «_Программы - быстрый запуск/Anonymizer.app»
# Пересборка меняет подпись: macOS снова спросит доступ к «Рабочему столу» - собирать один раз на версию.
# Windows-сборка (build_windows.bat, Anonymizer.spec) - без изменений: решение Андрея 24.10.
set -euo pipefail
cd "$(dirname "$0")"
VER="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' core/version.py)"
bash common/app/build_app.sh --name Anonymizer --id local.andrey.anonymizer --dest . --script desktop/launcher.sh \
  --icon assets/icon_1024.png --version "$VER"
echo "Готово: $(pwd)/Anonymizer.app ($VER)"
