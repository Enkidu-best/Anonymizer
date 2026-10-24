#!/usr/bin/env bash
# Что запущено.
PORT="${ANONYMIZER_PORT:-8070}"
row() { printf "  %-28s %s\n" "$1" "$2"; }
echo "Anonymizer"
if info="$(curl -sf "http://127.0.0.1:$PORT/api/info")"; then row "Окно (:$PORT)" "работает · $info"; else row "Окно (:$PORT)" "не запущено"; fi
if st="$(curl -sf "http://127.0.0.1:$PORT/api/status")"; then row "Распознавание имён" "$(echo "$st" | python3 -c 'import json,sys; n=json.load(sys.stdin)["ner"]; print("готово" if n["ready"] else "загружается" if n["loading"] else "ошибка")')"; fi
curl -sf "${ANONYMIZER_OLLAMA_URL:-http://localhost:11434}/api/tags" >/dev/null && row "Ollama (если включена)" "работает" || row "Ollama (если включена)" "не запущена"
