"""End-to-end run on the owner's real documents — locally only (task 2, §0.2).

    python scripts/e2e_private.py ["Проверка распознавания текста"] [--llm] [--only .docx]

Every file goes through the app's HTTP API exactly like the UI: process, download,
independent re-read, checks, restore, LLM-like reply, mapping edits, journal.
Report: private/audit_<date>/report.md — counts, place numbers, error kinds, times.
NO values and NO file names in the report (files are numbered).
"""
import argparse
import datetime
import importlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder', nargs='?', default=str(ROOT / 'Проверка распознавания текста'))
    ap.add_argument('--llm', action='store_true', help='also the «Точно» mode (Ollama)')
    ap.add_argument('--only', default='', help='only files with this extension')
    ap.add_argument('--model', default='qwen3.5:9b', help='Ollama model for «Точно»')
    ap.add_argument('--llm-only', action='store_true', help='only the «Точно» mode')
    ap.add_argument('--rewrite', action='store_true',
                    help='only the live «someone else\'s text» check: the LLM rewrites each DOCX into a memo')
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix='anon_e2e_private_'))
    os.environ['ANONYMIZER_DATA_DIR'] = str(work / 'data')
    os.environ['ANONYMIZER_LOG_DIR'] = str(work / 'logs')
    app = importlib.import_module('app')
    import core.anonymizer as A
    A._do_load()
    import core.llm as L
    L.check_ollama()
    L.set_model(args.model)
    from tests.e2e.runner import Client, run_document
    from core.handlers import ALLOWED_EXTENSIONS
    client = Client(app.app)

    files = sorted(f for f in Path(args.folder).iterdir()
                   if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS and not f.name.startswith('.')
                   and (not args.only or f.suffix.lower() == args.only))
    if args.rewrite:
        return _rewrite(client, files, work)
    modes = ([] if args.llm_only else [('Быстро', True, False)]) + \
        ([('Точно', True, True)] if args.llm or args.llm_only else [])
    out = ROOT / 'private' / f'audit_{datetime.date.today():%Y-%m-%d}'
    out.mkdir(parents=True, exist_ok=True)

    rows, totals = [], {'files': 0, 'errors': 0}
    for mode, spacy, llm in modes:
        for n, f in enumerate(files, 1):
            t = time.time()
            try:
                rep = run_document(client, f, work / mode / str(n), spacy=spacy, llm=llm,
                                   log_dir=work / 'logs')
            except Exception as ex:
                rep = {'ext': f.suffix.lower(), 'errors': [f'crash:{type(ex).__name__}'], 'found': {}, 'times': {}}
            rep['total_s'] = round(time.time() - t, 1)
            errs = rep['errors'] + rep.get('errors_llm', []) + rep.get('errors_edits', [])
            rows.append((mode, n, rep, errs))
            totals['files'] += 1
            totals['errors'] += len(errs)
            if llm:
                import core.llm as L
                L.set_model(args.model)
            print(f'{mode:6} #{n:<3} {rep["ext"]:6} {rep["total_s"]:6.1f}s  llm={rep.get("llm", "")} '
                  f'review_s={rep["times"].get("llm_review", "")} errors={len(errs):<3} '
                  f'found={sum(rep["found"].values()):<4} {", ".join(errs)[:120]}', flush=True)

    lines = [f'# Сквозной прогон личного набора — {datetime.datetime.now():%d.%m.%Y %H:%M}', '',
             'Значения и имена файлов в отчёт не пишутся: файлы пронумерованы по алфавиту папки.', '',
             f'Файлов: {len(files)}, режимы: {", ".join(m for m, *_ in modes)}, '
             f'ошибок всего: **{totals["errors"]}**', '',
             '| Режим | № | Тип | Найдено по типам | Время обработки, с | Всего, с | Ошибки |',
             '|---|---|---|---|---|---|---|']
    for mode, n, rep, errs in rows:
        found = ', '.join(f'{k} {v}' for k, v in sorted(rep['found'].items(), key=lambda x: -x[1]))
        llm_s = rep['times'].get('llm_review', '')
        lines.append(f'| {mode} | {n} | {rep["ext"]} | {found} | {rep["times"].get("anonymize", "")}'
                     f'{" / ИИ " + str(llm_s) if llm_s else ""} | '
                     f'{rep["total_s"]} | {"; ".join(errs) or "—"} |')
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (out / 'report.json').write_text(json.dumps([r for *_, r, __ in [(m, n, rep, e) for m, n, rep, e in rows]],
                                                ensure_ascii=False, indent=1), encoding='utf-8')
    # §1.1: after processing the base must hold no group the automatic rules would merge
    import sqlite3
    from scripts.audit_db import groups, proposals
    con = sqlite3.connect(f'file:{app.DB_PATH}?mode=ro', uri=True)
    rows = con.execute("SELECT session_id, token, entity_type, original_form FROM mappings "
                       "WHERE status='active'").fetchall()
    dup, prop = groups(rows), sum(len(v) for v in proposals(rows).values())
    print(f'Дубли в базе после прогона: автоматически {len(dup)}, предложить человеку {prop}')
    totals['errors'] += len(dup)
    print(f'\nОтчёт: {out / "report.md"}  ошибок: {totals["errors"]}')
    return 1 if totals['errors'] else 0


def _rewrite(client, files, work):
    """Task 3, §2.5.2: no values or names in the output, numbers only."""
    from tests.e2e.rewrite import ollama_writer, run_rewrite
    import core.llm as L
    if not L.check_ollama().get('available'):
        print('Ollama недоступна — живой вариант пропущен')
        return 0
    write, errors = ollama_writer(), 0
    for n, f in enumerate(files, 1):
        if f.suffix.lower() != '.docx':
            continue
        t = time.time()
        try:
            rep = run_rewrite(client, f, work / 'rewrite' / str(n), write)
        except Exception as ex:
            rep = {'errors': [f'crash:{type(ex).__name__}'], 'names': 0, 'orgs': 0, 'invented': 0}
        errors += len(rep['errors'])
        print(f'Записка #{n:<3} {time.time() - t:6.1f}s имён в новом тексте={rep["names"]:<3} '
              f'организаций={rep["orgs"]:<3} выдуманных токенов={rep["invented"]:<3} '
              f'ошибки={len(rep["errors"])} {", ".join(rep["errors"])}', flush=True)
    print(f'Ошибок: {errors}')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
