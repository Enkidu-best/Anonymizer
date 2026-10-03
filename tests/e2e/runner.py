"""End-to-end scenario «as the user does it» through the app's HTTP API (task 2, §0.1).

For one document: process → download → independent re-read → checks → deanonymize the
downloaded file → compare place by place with the original → LLM-like reply → mapping
edits with reprocess → journal without personal data.

The result contains only counts, place numbers and error kinds — never values — so the
report of the private run can be shown and committed safely.
"""
import io
import random
import re
import time
from collections import Counter
from pathlib import Path

from tests.e2e.independent import places, norm

TOKEN_RE = re.compile(r'\[([A-Z]+_\d+)\]')
_WORDS = re.compile(r'\[[A-Z]+_\d+\]|\w+')
OFFICE = {'.docx', '.docm', '.pptx'}
LAYOUT = {'.pdf', '.jpg', '.jpeg', '.png', '.heic', '.tif', '.tiff', '.bmp', '.webp'}


class Client:
    """Thin wrapper over the Flask test client: the same requests the UI sends."""

    def __init__(self, app):
        self.c = app.test_client()

    def json(self, method, url, **kw):
        r = getattr(self.c, method)(url, **kw)
        assert r.status_code < 400, f'{method.upper()} {url} → {r.status_code}'
        return r.get_json()

    def session(self, name='e2e'):
        return self.json('post', '/api/sessions', json={'name': name})['id']

    def process(self, sid, path: Path, mode='anonymize', spacy=True, llm=False, name=None):
        data = {'mode': mode, 'session_id': sid, 'use_spacy': str(spacy).lower(),
                'use_llm': str(llm).lower(), 'engine': 'accurate' if llm else 'fast',
                'files': (io.BytesIO(Path(path).read_bytes()), name or Path(path).name)}
        d = self.json('post', '/api/process', data=data, content_type='multipart/form-data')
        r = d['results'][0]
        assert r['status'] == 'ok', r.get('error')
        return r

    def download(self, sid, out_name, dest: Path) -> Path:
        r = self.c.get(f'/api/download/{sid}/{out_name}')
        assert r.status_code == 200, f'download {r.status_code}'
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.data)
        return dest

    def mappings(self, sid, archive=False):
        return self.json('get', f'/api/sessions/{sid}/mappings' + ('?all=1' if archive else ''))


def _present(text: str, value: str) -> bool:
    v = norm(value)
    if len(v) < 3:
        return False
    pre = r'(?<!\d)' if v[0].isdigit() else r'(?<![\w])'
    post = r'(?!\d)' if v[-1].isdigit() else r'(?![\w])'
    return bool(re.search(pre + re.escape(v) + post, text))


def run_document(client: Client, src: Path, work: Path, spacy=True, llm=False, log_dir: Path = None,
                 edits=True) -> dict:
    """All checks for one file. Returns a report dict (no personal data)."""
    from core.entities import value_key
    from core.lexicon import not_pii
    ext = src.suffix.lower()
    rep = {'ext': ext, 'errors': [], 'found': {}, 'times': {}}
    work.mkdir(parents=True, exist_ok=True)
    sid = client.session('Сессия 01.10.26')

    # 1-2. process and download exactly like the UI
    t = time.time()
    r = client.process(sid, src, spacy=spacy, llm=llm)
    rep['times']['anonymize'] = round(time.time() - t, 2)
    if llm and r.get('llm_job'):
        # «Точно»: wait for the background review, apply everything it proposes
        st = {}
        for _ in range(600):
            st = client.json('get', f'/api/llm-jobs/{r["llm_job"]}')
            if st['status'] != 'running':
                break
            time.sleep(0.5)
        rep['times']['llm_review'] = round(time.time() - t, 1)
        pr = st.get('proposal') or {}
        rep['llm'] = {'status': st.get('status'), 'add': len(pr.get('add', [])),
                      'remove': len(pr.get('remove', [])), 'merge': len(pr.get('merge', []))}
        if st.get('status') == 'done' and (pr.get('add') or pr.get('remove') or pr.get('merge')):
            res = client.json('post', f'/api/llm-jobs/{r["llm_job"]}/apply',
                              json={'add': list(range(len(pr['add']))),
                                    'remove': [x['token'] for x in pr['remove']],
                                    'merge': [x['token'] for x in pr['merge']]})
            r = dict(r, output=res['output'])
        elif st.get('status') != 'done':
            rep['errors'].append(f'llm_review:{st.get("status")}')
    anon = client.download(sid, r['output'], work / ('anon' + Path(r['output']).suffix))
    out_ext = Path(r['output']).suffix.lower()
    # a PDF with a text layer comes back as Word (task 3, §4): check it as Word
    layout = ext in LAYOUT and out_ext != '.docx'
    maps = client.mappings(sid)
    rep['found'] = dict(Counter(m['token'].rsplit('_', 1)[0] for m in {m['token']: m for m in maps}.values()))

    # 3. independent re-read of the downloaded file
    got = places(anon)
    body = '\n'.join(norm(x) for x in got['body'])
    meta = '\n'.join(norm(x) for x in got['meta'])
    ocr = '\n'.join(norm(x) for x in got.get('ocr', []))   # text seen on page images
    alltext = body + '\n' + meta + '\n' + ocr + '\n' + norm(r['output'])

    # 4a. no original value (or its line parts) anywhere: body, metadata, file name
    for m in maps:
        vals = [m['original_form']] + [p for p in m['original_form'].split('\n') if len(p.strip()) >= 4]
        if any(_present(alltext, v) for v in vals):
            rep['errors'].append(f'leak:{m["token"]}:{m["entity_type"]}')
    # 4b. every token of the file is known to the session
    known = {m['token'] for m in client.mappings(sid, archive=True)}
    unknown = set(TOKEN_RE.findall(body)) - known
    if unknown:
        rep['errors'].append(f'unknown_tokens:{len(unknown)}')
    # 4c. one value = one token
    keys = {}
    for m in maps:
        keys.setdefault((m['entity_type'], value_key(m['original_form'], m['entity_type'])), set()).add(m['token'])
    dup = [k for k, v in keys.items() if len(v) > 1]
    if dup:
        rep['errors'].append(f'one_value_many_tokens:{len(dup)}')
    # 4d. roles, positions, headings, public bodies are not masked
    from core.lexicon import is_role_or_position, is_public_body
    roles = [m['token'] for m in maps if m['entity_type'] in ('ФИО', 'ЮЛ') and
             (is_role_or_position(m['original_form']) or is_public_body(m['original_form']))]
    if roles:
        rep['errors'].append(f'masked_roles:{len(roles)}')
    # 4e. preview (what the user looks at) == downloaded file
    pv = client.json('get', f'/api/sessions/{sid}/preview/{r["output"]}')
    if not layout:
        # same words and tokens; line breaks inside a paragraph may be split differently
        pv_words = Counter(_WORDS.findall(norm(pv['text'])))
        file_words = Counter(_WORDS.findall(norm('\n'.join(got['body']))))
        diff = (pv_words - file_words) + (file_words - pv_words)
        if diff:
            rep['errors'].append(f'preview_differs:{sum(diff.values())}_words')

    # 5-6. deanonymize the downloaded file, compare place by place with the original
    if not layout:
        t = time.time()
        r2 = client.process(sid, anon, mode='deanonymize', name=r['output'])
        rep['times']['restore'] = round(time.time() - t, 2)
        back = client.download(sid, r2['output'], work / ('back' + Path(r2['output']).suffix))
        if ext == '.pdf':
            from core.handlers import _pdf_to_docx
            base = _pdf_to_docx(src, work)
        else:
            base = src if ext not in ('.doc', '.odt') else _converted(src, work)
        orig_p = [norm(x) for x in places(base)['body']]
        back_p = [norm(x) for x in places(back)['body']]
        if len(orig_p) != len(back_p):
            rep['errors'].append(f'restore_places_count:{len(orig_p)}→{len(back_p)}')
        for i, (a, b) in enumerate(zip(orig_p, back_p)):
            if a != b:
                kind = 'token_left' if TOKEN_RE.search(b) else 'text_differs'
                rep['errors'].append(f'restore:{kind}:place{i}')
        if r2.get('restore', {}).get('unknown'):
            rep['errors'].append(f'restore_unknown:{len(r2["restore"]["unknown"])}')

        # 7. reply of an external LLM: reordered, dropped, repeated and damaged tokens
        if (ext in OFFICE | {'.txt'} or out_ext == '.docx') and TOKEN_RE.search(body):
            rep.update(_llm_reply(client, sid, anon, work))

        # 8. mapping edits: excluded / edited entries keep old files restorable
        if edits and maps:
            rep.update(_edits(client, sid, src, anon, r, orig_p, work))
    elif ext == '.pdf':
        # layout format: the restored PDF has no tokens left in its text layer
        r2 = client.process(sid, anon, mode='deanonymize', name=r['output'])
        back = client.download(sid, r2['output'], work / 'back.pdf')
        import pymupdf
        txt = '\n'.join(p.get_text() for p in pymupdf.open(str(back)))
        if TOKEN_RE.search(txt):
            rep['errors'].append(f'restore:token_left:{len(TOKEN_RE.findall(txt))}')

    # 9. journal without personal data
    if log_dir is not None:
        jtext = ''
        for f in Path(log_dir).rglob('*'):
            if f.is_file():
                jtext += f.read_text(encoding='utf-8', errors='replace')
        jtext = norm(jtext)
        hits = [m['token'] for m in client.mappings(sid, archive=True)
                if len(m['original_form']) >= 4 and norm(m['original_form']) in jtext]
        if hits:
            rep['errors'].append(f'journal_contains_values:{len(hits)}')
        if norm(src.stem) and len(src.stem) > 6 and norm(src.stem) in jtext:
            rep['errors'].append('journal_contains_file_name')
        if 'job_end' not in jtext:
            rep['errors'].append('journal_no_job_summary')
    return rep


def _converted(src, work):
    from core.handlers import _convert_to_docx
    return _convert_to_docx(src, work)


def _llm_reply(client, sid, anon, work):
    """A docx like an external LLM would return it, then restore it."""
    from docx import Document
    paras = [p for p in places(anon)['body'] if TOKEN_RE.search(p)]
    rnd = random.Random(7)
    kept = [p for p in paras if rnd.random() > 0.3]
    rnd.shuffle(kept)
    toks = TOKEN_RE.findall(' '.join(paras))[:3]
    damaged = [f'[{t.replace("_", " ")}]' for t in toks[:1]] + [t for t in toks[1:2]] + [f'[{t.lower()}]' for t in toks[2:3]]
    new_par = 'Вывод: ' + ', '.join(damaged) + ' — риски по сделке.'
    d = Document()
    for p in kept + [new_par]:
        d.add_paragraph(p)
    reply = work / 'llm_reply.docx'
    d.save(reply)
    r = client.process(sid, reply, mode='deanonymize', name='llm_reply.docx')
    back = client.download(sid, r['output'], work / 'llm_back.docx')
    text = '\n'.join(places(back)['body'])
    out = {'llm_reply': {'paragraphs': len(kept) + 1, 'check_case': r.get('restore', {}).get('check_case', 0)}}
    from core.anonymizer import TOKEN_LOOSE_RE
    if TOKEN_LOOSE_RE.search(text):
        out['errors_llm'] = ['llm_reply:token_left']
    return out


def _edits(client, sid, src, anon_old, r_old, orig_p, work):
    """Delete one entry and edit another, reprocess, then restore BOTH the old and the new file."""
    out = {'errors_edits': []}
    maps = client.mappings(sid)
    by_token = {}
    for m in maps:
        by_token.setdefault(m['token'], m)
    tokens = sorted(by_token)
    if not tokens:
        return out
    victim = tokens[0]
    client.json('delete', f'/api/sessions/{sid}/mappings/{victim}')
    if len(tokens) > 1:
        t2 = tokens[-1]
        client.json('patch', f'/api/sessions/{sid}/mappings/{t2}',
                    json={'canonical_form': by_token[t2]['original_form'] + ' (изм.)'})
    client.json('post', f'/api/sessions/{sid}/reprocess')
    files = client.json('get', f'/api/sessions/{sid}/files')['files']
    new_anon_name = next((f['output'] for f in files if f['output'].endswith(anon_old.suffix)
                          and '_anon' in f['output']), None)
    if new_anon_name:
        new_anon = client.download(sid, new_anon_name, work / ('anon2' + anon_old.suffix))
        if victim in TOKEN_RE.findall('\n'.join(places(new_anon)['body'])):
            out['errors_edits'].append('excluded_token_still_used')
    # the file downloaded BEFORE the edits must still restore exactly
    r = client.process(sid, anon_old, mode='deanonymize', name=r_old['output'])
    back = client.download(sid, r['output'], work / ('back_old' + anon_old.suffix))
    back_p = [norm(x) for x in places(back)['body']]
    bad = sum(1 for a, b in zip(orig_p, back_p) if a != b) + abs(len(orig_p) - len(back_p))
    if bad:
        out['errors_edits'].append(f'old_file_after_edits:{bad}_places')
    return out
