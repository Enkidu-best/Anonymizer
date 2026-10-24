"""«Только нейросеть» - не режим окна, а команда для замеров качества модели (описание 5.5, вопрос 4).

Обезличивает файлы одной нейросетью (правила выключены) и печатает, сколько значений из таблицы замен правил
нашла и пропустила нейросеть - только числа, без значений. Нужна Ollama; видеокарта - под общей очередью,
запуск Claude Code - с HEAVY_LOCK_WHO=background.

    HEAVY_LOCK_WHO=background .venv312/bin/python scripts/llm_only_eval.py <файл или папка> [--model qwen3.5:9b]
"""
import argparse
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('path')
    ap.add_argument('--model', default='')
    a = ap.parse_args()
    work = Path(tempfile.mkdtemp(prefix='anon_llm_only_'))
    os.environ.setdefault('ANONYMIZER_DATA_DIR', str(work))
    from core.db import init_db, create_session, get_session_mappings
    from core.handlers import process_uploaded_file, ALLOWED_EXTENSIONS
    from core import llm, heavy
    llm.check_ollama()
    if a.model:
        llm.set_model(a.model)
    src = Path(a.path)
    files = [src] if src.is_file() else sorted(f for f in src.iterdir() if f.suffix.lower() in ALLOWED_EXTENSIONS)
    db = work / 'anon.db'
    init_db(db)
    total_rules = total_found = 0
    for f in files:
        rules_sid, llm_sid = create_session(db, 'rules'), create_session(db, 'llm')
        process_uploaded_file(f, work / 'rules', rules_sid, db, 'anonymize', use_spacy=True)
        with heavy.job('wait', 'замер «только нейросеть»', wait=1800):
            process_uploaded_file(f, work / 'llm', llm_sid, db, 'anonymize', use_spacy=False, use_llm=True, llm_only=True)
        rules = {m['original_form'] for m in get_session_mappings(db, rules_sid)}
        found = {m['original_form'] for m in get_session_mappings(db, llm_sid)}
        hit = len(rules & found)
        total_rules += len(rules)
        total_found += hit
        print(f'{f.suffix}: правила {len(rules)}, нейросеть нашла из них {hit}, ещё своих {len(found - rules)}')
    if total_rules:
        print(f'Итого: нейросеть нашла {total_found} из {total_rules} ({100 * total_found // total_rules} %)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
