"""Audit a folder of real documents locally: anonymize → leak check → deanonymize.

    python scripts/audit_folder.py "Проверка распознавания текста" [--ner] [--out private/audit]

For every file writes to --out (git-ignored by default):
  <name>.anon.txt   full text of the anonymized output (incl. metadata)
  <name>.map.txt    token → original, for manual review of what was masked
and report.md with status, time, entity counts, leaks and round-trip result.

Leak = an original value from the mapping still present in the output, or a
detector hit in the output that is not a token. Nothing leaves the machine.
"""
import argparse
import contextlib
import io
import re
import shutil
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _norm(s):
    s = s.replace('\xad', '-').replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', s).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('folder')
    ap.add_argument('--out', default=str(ROOT / 'private' / 'audit'))
    ap.add_argument('--ner', action='store_true')
    args = ap.parse_args()

    from core.db import init_db, create_session, get_session_mappings
    from core.handlers import process_uploaded_file, IMAGE_EXT, CONVERT_EXT
    from core.extract import extract_text
    from core.detectors import find_all
    from core.anonymizer import contains_bounded, ANY_TOKEN_RE, TOKEN_LOOSE_RE
    if args.ner:
        import core.anonymizer as A
        A._do_load()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp())
    db = work / 'audit.db'
    init_db(db)

    rows = []
    for src in sorted(Path(args.folder).iterdir()):
        if src.name.startswith('.') or not src.is_file():
            continue
        sid = create_session(db, src.name)
        d_in, d_anon, d_back = work / sid / 'in', work / sid / 'anon', work / sid / 'back'
        for d in (d_in, d_anon, d_back):
            d.mkdir(parents=True)
        f = d_in / src.name
        shutil.copy2(src, f)
        row = {'file': src.name, 'status': '', 'time': 0, 'types': '', 'leaks': [], 'rt': '',
               'out': ''}
        t0 = time.time()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                r = process_uploaded_file(f, d_anon, sid, db, 'anonymize', use_spacy=args.ner)
        except Exception as ex:
            row['status'] = f'отклонён: {ex}'
            rows.append(row)
            continue
        row['time'] = time.time() - t0
        anon = d_anon / r['output_filename']
        row['out'] = r['output_filename']
        maps = get_session_mappings(db, sid)
        row['types'] = ', '.join(f'{k}={v}' for k, v in Counter(m['entity_type'] for m in maps).most_common())
        try:
            anon_text = extract_text(anon) if f.suffix.lower() not in IMAGE_EXT else ''
        except Exception as ex:
            row['status'] = f'выход не читается: {ex}'
            rows.append(row)
            continue
        (out / f'{src.name}.anon.txt').write_text(anon_text, encoding='utf-8')
        (out / f'{src.name}.map.txt').write_text(
            '\n'.join(f"{m['token']:<12} {m['entity_type']:<10} {m['original_form']}" for m in maps),
            encoding='utf-8')
        row['leaks'] = sorted({f"{l['type']}: {l['value']}" for l in r.get('leaks', [])})
        row['out'] += f" ocr={r.get('ocr_pages', 0)}" if r.get('ocr_pages') else ''
        try:
            ext = f.suffix.lower()
            if ext in IMAGE_EXT:
                row['rt'] = 'н/п (изображение)'
            else:
                with contextlib.redirect_stdout(io.StringIO()):
                    r2 = process_uploaded_file(anon, d_back, sid, db, 'deanonymize')
                back = extract_text(d_back / r2['output_filename'])
                if ext == '.pdf' or ext in CONVERT_EXT:
                    # layout-based formats: no tokens left and every value restored
                    left = TOKEN_LOOSE_RE.findall(back)
                    missing = [m['original_form'] for m in maps
                               if _norm(m['original_form']) not in _norm(back)]
                    row['rt'] = 'OK' if not left and not missing else f'токенов {len(left)}, не восстановлено {len(missing)}'
                    if row['rt'] != 'OK':
                        (out / f'{src.name}.rt.diff.txt').write_text('\n'.join(missing[:50]), encoding='utf-8')
                else:
                    orig = extract_text(f, meta=False)
                    back = extract_text(d_back / r2['output_filename'], meta=False)
                    row['rt'] = 'OK' if _norm(back) == _norm(orig) else 'расхождение'
                    if row['rt'] != 'OK':
                        (out / f'{src.name}.rt.diff.txt').write_text(_first_diff(orig, back), encoding='utf-8')
        except Exception as ex:
            row['rt'] = f'ошибка: {ex}'
        row['status'] = 'ok'
        rows.append(row)
        print(f"{src.name[:55]:55} {row['time']:5.1f}s leaks={len(row['leaks']):<3} rt={row['rt']:<6} → {row['out'][:60]}")

    lines = ['# Аудит папки', '', f'Папка: `{args.folder}`, NER: {"да" if args.ner else "нет"}', '',
             '| Файл | Статус | Время, с | Найдено | Утечки | Обратная замена | Выходной файл |', '|---|---|---|---|---|---|---|']
    for r in rows:
        lines.append(f"| {r['file']} | {r['status']} | {r['time']:.1f} | {r['types']} | "
                     f"{len(r['leaks'])} | {r['rt']} | {r['out']} |")
    lines.append('')
    for r in rows:
        if r['leaks']:
            lines.append(f"## {r['file']}")
            lines += [f'- {x}' for x in r['leaks'][:80]]
            lines.append('')
    (out / 'report.md').write_text('\n'.join(lines), encoding='utf-8')
    print(f'\nОтчёт: {out / "report.md"}')


def _first_diff(a, b):
    a, b = _norm(a), _norm(b)
    i = next((k for k in range(min(len(a), len(b))) if a[k] != b[k]), min(len(a), len(b)))
    return f'ОРИГИНАЛ: …{a[max(0, i - 200):i + 300]}\n\nПОСЛЕ:    …{b[max(0, i - 200):i + 300]}'


if __name__ == '__main__':
    main()
