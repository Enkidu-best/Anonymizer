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
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix='anon_e2e_private_'))
    os.environ['ANONYMIZER_DATA_DIR'] = str(work / 'data')
    os.environ['ANONYMIZER_LOG_DIR'] = str(work / 'logs')
    app = importlib.import_module('app')
    import core.anonymizer as A
    A._do_load()
    from tests.e2e.runner import Client, run_document
    from core.handlers import ALLOWED_EXTENSIONS
    client = Client(app.app)

    files = sorted(f for f in Path(args.folder).iterdir()
                   if f.is_file() and f.suffix.lower() in ALLOWED_EXTENSIONS and not f.name.startswith('.')
                   and (not args.only or f.suffix.lower() == args.only))
    modes = [('Быстро', True, False)] + ([('Точно', True, True)] if args.llm else [])
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
            print(f'{mode:6} #{n:<3} {rep["ext"]:6} {rep["total_s"]:6.1f}s  errors={len(errs):<3} '
                  f'found={sum(rep["found"].values()):<4} {", ".join(errs)[:120]}', flush=True)

    lines = [f'# Сквозной прогон личного набора — {datetime.datetime.now():%d.%m.%Y %H:%M}', '',
             'Значения и имена файлов в отчёт не пишутся: файлы пронумерованы по алфавиту папки.', '',
             f'Файлов: {len(files)}, режимы: {", ".join(m for m, *_ in modes)}, '
             f'ошибок всего: **{totals["errors"]}**', '',
             '| Режим | № | Тип | Найдено по типам | Время обработки, с | Всего, с | Ошибки |',
             '|---|---|---|---|---|---|---|']
    for mode, n, rep, errs in rows:
        found = ', '.join(f'{k} {v}' for k, v in sorted(rep['found'].items(), key=lambda x: -x[1]))
        lines.append(f'| {mode} | {n} | {rep["ext"]} | {found} | {rep["times"].get("anonymize", "")} | '
                     f'{rep["total_s"]} | {"; ".join(errs) or "—"} |')
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    (out / 'report.json').write_text(json.dumps([r for *_, r, __ in [(m, n, rep, e) for m, n, rep, e in rows]],
                                                ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'\nОтчёт: {out / "report.md"}  ошибок: {totals["errors"]}')
    return 1 if totals['errors'] else 0


if __name__ == '__main__':
    sys.exit(main())
