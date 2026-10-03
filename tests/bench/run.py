"""Benchmark: how many real-world variants the detector masks.

Run from the repo root:
    python tests/bench/run.py            # regex + default detectors
    python tests/bench/run.py --spacy    # also load the NER layer (v2.3 API)

Prints a per-category table and writes tests/bench/fails.json with every miss.
A case passes when every `must_mask` string is gone from the output, every
`must_keep` string survived, and deanonymization restores the input exactly.

When the pipeline API changes, update only `anonymize()` below.
"""
import collections, contextlib, io, json, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from corpus import C  # noqa: E402

USE_NER = '--spacy' in sys.argv or '--ner' in sys.argv


def anonymize(text):
    """Adapter for the v2.3 API. Returns (masked_text, restored_text)."""
    from core.db import init_db, create_session
    from core.anonymizer import anonymize_text, restore_text
    db = os.path.join(tempfile.mkdtemp(), 'bench.db')
    init_db(db)
    sid = create_session(db, 'bench')
    out, occ = anonymize_text(text, db, sid, use_spacy=USE_NER, use_llm=False)
    return out, restore_text(out, db, sid, occ)


def _tail_left(value, text, out, keep):
    """Part of a masked number left next to its token («[CAD_3]- 71/022/2020-18»): a run of
    3+ digits of the value still in the output (outside tokens and the must-keep strings)."""
    import re
    rest = re.sub(r'\[[A-Z]+_\d+\]', ' ', out)
    other = text.replace(value, ' ')     # the same digits elsewhere in the input («доб. 123»)
    for k in keep:
        rest, other = rest.replace(k, ' '), other.replace(k, ' ')
    return any(run in rest and run not in other for run in re.findall(r'\d{3,}', value))


def main():
    if USE_NER:
        import core.anonymizer as A
        A._do_load()
    stats = collections.defaultdict(lambda: [0, 0, 0, 0])
    fails = []
    for cat, text, mask, keep in C:
        with contextlib.redirect_stdout(io.StringIO()):
            out, back = anonymize(text)
        leaked = [m for m in mask if m in out or _tail_left(m, text, out, keep)]
        lost = [k for k in keep if k not in out]
        s = stats[cat]
        s[0] += 1
        s[1] += not leaked and not lost
        s[2] += bool(lost)
        s[3] += back != text
        if leaked or lost or back != text:
            fails.append({'cat': cat, 'in': text, 'out': out, 'leaked': leaked,
                          'lost': lost, 'roundtrip_ok': back == text})
    print(f"{'Category':<12} {'cases':>6} {'ok':>4} {'%':>5} {'FP':>4} {'RT':>4}")
    for k, v in stats.items():
        print(f'{k:<12} {v[0]:>6} {v[1]:>4} {100 * v[1] // v[0]:>4}% {v[2]:>4} {v[3]:>4}')
    t = [sum(v[i] for v in stats.values()) for i in range(4)]
    print(f"{'TOTAL':<12} {t[0]:>6} {t[1]:>4} {100 * t[1] // t[0]:>4}% {t[2]:>4} {t[3]:>4}")
    print('FP = cases where something that must stay was masked; RT = round-trip failures')
    with open(os.path.join(HERE, 'fails.json'), 'w', encoding='utf-8') as f:
        json.dump(fails, f, ensure_ascii=False, indent=1)
    return t


if __name__ == '__main__':
    main()
