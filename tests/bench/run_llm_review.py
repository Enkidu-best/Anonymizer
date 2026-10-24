"""Стенд 245 с галочкой «Использовать нейросеть» (4.0, «_Порядок 03», п. 5): правила + модель имён, затем проверка
нейросетью (`core/llm_review.review`) и применение её предложений так, как они отмечены в окне по умолчанию (3.15):
«Замаскировать» и «Объединить» - да, «Снять маску» - нет. Полнота, FP, RT - как в `run.py`; плюс время проверки.

    HEAVY_LOCK_WHO=background .venv312/bin/python tests/bench/run_llm_review.py [--model qwen3.5:9b]

Каждый случай - под общей очередью видеокарты отдельно: программа Андрея, если ждёт, получает видеокарту между случаями.
"""
import collections
import contextlib
import io
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
os.environ.setdefault('ANONYMIZER_DATA_DIR', tempfile.mkdtemp(prefix='anon_bench_llm_'))

from corpus import C  # noqa: E402
from run import _tail_left  # noqa: E402


def main():
    model = sys.argv[sys.argv.index('--model') + 1] if '--model' in sys.argv else 'qwen3.5:9b'
    import core.anonymizer as A
    from core import heavy, llm
    from core.db import init_db, create_session, get_session_mappings, get_or_create_token, merge_tokens
    from core.anonymizer import anonymize_text, restore_text
    from core.llm_review import review
    A._do_load()
    llm.set_model(model)
    llm.check_ollama()
    stats = collections.defaultdict(lambda: [0, 0, 0, 0])
    totals = {'add': 0, 'remove': 0, 'merge': 0, 'partial': 0, 'seconds': 0.0}
    fails, t_all = [], time.time()
    for n, (cat, text, mask, keep) in enumerate(C, 1):
        db = os.path.join(tempfile.mkdtemp(), 'bench.db')
        init_db(db)
        sid = create_session(db, 'bench')
        with contextlib.redirect_stdout(io.StringIO()):
            out, _ = anonymize_text(text, db, sid, use_spacy=True, use_llm=False)
            t0 = time.time()
            with heavy.job('wait', 'стенд: проверка нейросетью', wait=3600):
                p = review(text, out, get_session_mappings(db, sid))
            totals['seconds'] += time.time() - t0
            for a in p['add']:
                get_or_create_token(db, sid, a['value'], a['value'], a['type'], manual=True)
            for m in p['merge']:
                merge_tokens(db, sid, m['token'], m['into'])
            out, occ = anonymize_text(text, db, sid, use_spacy=True, use_llm=False)
            back = restore_text(out, db, sid, occ)
        for k in ('add', 'remove', 'merge'):
            totals[k] += len(p[k])
        totals['partial'] += bool(p.get('partial'))
        leaked = [m for m in mask if m in out or _tail_left(m, text, out, keep)]
        lost = [k for k in keep if k not in out]
        s = stats[cat]
        s[0] += 1
        s[1] += not leaked and not lost
        s[2] += bool(lost)
        s[3] += back != text
        if leaked or lost or back != text:
            fails.append({'cat': cat, 'in': text, 'out': out, 'leaked': leaked, 'lost': lost, 'roundtrip_ok': back == text,
                          'proposal': {k: p[k] for k in ('add', 'remove', 'merge')}})
        if n % 25 == 0:
            print(f'{n}/{len(C)} · {time.time() - t_all:.0f} с', flush=True)
    print(f"{'Category':<12} {'cases':>6} {'ok':>4} {'%':>5} {'FP':>4} {'RT':>4}")
    for k, v in stats.items():
        print(f'{k:<12} {v[0]:>6} {v[1]:>4} {100 * v[1] // v[0]:>4}% {v[2]:>4} {v[3]:>4}')
    t = [sum(v[i] for v in stats.values()) for i in range(4)]
    print(f"{'TOTAL':<12} {t[0]:>6} {t[1]:>4} {100 * t[1] // t[0]:>4}% {t[2]:>4} {t[3]:>4}")
    print(f"Нейросеть ({model}): предложено найти ещё {totals['add']}, снять {totals['remove']} (не применялось), "
          f"объединить {totals['merge']}; не уложились в срок: {totals['partial']}; проверка - всего "
          f"{totals['seconds']:.0f} с, в среднем {totals['seconds'] / len(C):.1f} с на случай; весь прогон {time.time() - t_all:.0f} с")
    with open(os.path.join(HERE, 'fails_llm.json'), 'w', encoding='utf-8') as f:
        json.dump(fails, f, ensure_ascii=False, indent=1)


if __name__ == '__main__':
    main()
