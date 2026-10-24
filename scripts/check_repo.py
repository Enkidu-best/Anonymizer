"""Проверка репозитория перед отправкой на GitHub (стандарт 6.8, описание 8): то, что лежит в git, не раскрывает
документов и данных Андрея.

    .venv312/bin/python scripts/check_repo.py

Ищет в файлах git (рабочая версия и индекс): запрещённые пути (`*.db`, `uploads/`, `private/`, реальные документы,
архивы, `.app`), файлы больше 20 МБ, пути Mac пользователя (`/Users/…`), имена файлов и куски текста реальных
документов из «Проверка распознавания текста/» (папка вне git). Сами найденные строки не печатаются - только числа
и имена файлов репозитория. Код возврата 0 - чисто, 1 - найдено. Без папки документов (облако, чужая машина) -
предупреждение, проверка имён и текстов пропускается.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / 'Проверка распознавания текста'
MAX_MB = 20
FORBIDDEN = [r'(^|/)[^/]*\.db(-wal|-shm|-journal)?$', r'(^|/)uploads/', r'(^|/)private/', r'^Проверка распознавания текста/',
             r'\.bundle$', r'\.tar\.gz$', r'(^|/)\.env$', r'(^|/)Anonymizer\.app/', r'(^|/)\.run/', r'(^|/)logs/']
TEXT_EXT = {'.py', '.md', '.txt', '.json', '.js', '.html', '.css', '.sh', '.bat', '.spec', '.yml', '.yaml', '.toml', '.ini',
            '.cfg', '.c', '.conf', '.svg'}
ALLOWED_USERS = ('/Users/Shared', '/Users/runner')


def git_files() -> list[str]:
    out = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-z'], capture_output=True, check=True).stdout
    return [x for x in out.decode('utf-8').split('\0') if x]


def corpus_terms() -> list[str]:
    """Имена реальных документов (характерные) и куски их текста по 60 знаков."""
    terms = []
    if not SAMPLES.exists():
        return terms
    for p in sorted(SAMPLES.rglob('*')):
        if not p.is_file() or p.name.startswith('.'):
            continue
        stem = p.stem.strip()
        if len(stem) >= 8 and not stem.isdigit() and (re.search(r'\d|[«»"]|\b[А-ЯЁ][а-яё]+ов|ин\b', stem) or len(stem.split()) >= 3):
            terms.append(stem.lower())
        if p.suffix.lower() in ('.txt', '.docx', '.rtf'):
            try:
                sys.path.insert(0, str(ROOT))
                from core.extract import extract_text
                t = re.sub(r'\s+', ' ', extract_text(p, meta=False))
            except Exception:
                continue
            for j in range(200, min(len(t), 3000), 700):
                piece = t[j:j + 60].lower()
                if len(piece) == 60 and re.search(r'[а-яё]{4}', piece):
                    terms.append(piece)
    return terms


def main() -> int:
    files = git_files()
    bad_path = [f for f in files if any(re.search(rx, f) for rx in FORBIDDEN)]
    big = [f for f in files if (ROOT / f).is_file() and (ROOT / f).stat().st_size > MAX_MB * 1024 * 1024]
    users, hits = [], {}
    terms = corpus_terms()
    for f in files:
        p = ROOT / f
        if p.suffix.lower() not in TEXT_EXT or not p.is_file() or p.stat().st_size > 5_000_000:
            continue
        text = p.read_text(encoding='utf-8', errors='ignore')
        for m in re.finditer(r'/Users/[A-Za-z0-9._-]+', text):
            if not m.group(0).startswith(ALLOWED_USERS):
                users.append(f)
                break
        low = text.lower()
        n = sum(1 for t in terms if t in low)
        if n:
            hits[f] = n
    print(f'Файлов в git: {len(files)}')
    print(f'Запрещённые пути: {len(bad_path)}' + (f' - {bad_path[:5]}' if bad_path else ''))
    print(f'Файлы больше {MAX_MB} МБ: {len(big)}' + (f' - {big[:5]}' if big else ''))
    print(f'Пути пользователя Mac: {len(users)}' + (f' - {sorted(set(users))[:5]}' if users else ''))
    if SAMPLES.exists():
        print(f'Имена и тексты реальных документов: проверено строк {len(terms)}, найдено в {len(hits)} файлах'
              + (f' - {sorted(hits)[:5]}' if hits else ''))
    else:
        print('Предупреждение: нет папки реальных документов - имена и тексты не проверены')
    bad = bad_path or big or users or hits
    print('Итог: ' + ('НАЙДЕНО - отправлять нельзя' if bad else 'чисто'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
