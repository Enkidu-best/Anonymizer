# CLAUDE.md

Guidance for Claude Code in this repository. The development plan and working rules live in [CLAUDE_CODE_TASK.md](CLAUDE_CODE_TASK.md); detection rules per PII type live in [docs/PII_VARIANTS_CATALOG.md](docs/PII_VARIANTS_CATALOG.md). Read both before changing detection logic.

## Project

Local-only PII anonymizer/deanonymizer for Russian legal documents (152-FZ, banking and trade secrets). Flask backend + single-page frontend, packaged with PyInstaller. Formats: TXT, DOCX, PDF (text layer only), XLSX, RTF. The owner is a lawyer: talk to them in Russian, briefly, with results and what was verified.

Main metric is **recall**: a missed name leaks abroad, an extra mask only slightly hurts analysis.

## Commands

```bash
python3.12 -m venv .venv312 && .venv312/bin/pip install -r requirements-dev.txt   # Python 3.12
python app.py                          # native window; --browser / --server
python -m pytest -q                    # unit + format tests (UI tests need `playwright install chromium`)
python tests/bench/run.py              # recall bench, 200 variants, regex only
python tests/bench/run.py --ner        # same with spaCy
# tests/test_holdout_contract.py — contract the rules were NOT tuned on (overfitting guard)
./build_macos.sh / build_windows.bat   # dist/Anonymizer.app + .dmg / Windows folder
python scripts/audit_folder.py <dir>   # local audit of real documents → private/ (git-ignored)
```

Debug endpoints: `/api/status`, `/api/debug`, `/api/llm-status`.

## Working rules (from CLAUDE_CODE_TASK.md §0)

- Test first: add a failing case to `tests/bench/corpus.py` or `tests/test_*.py`, then fix.
- After every change: pytest green, bench % not lower than the last CHANGELOG entry, **FP = 0, RT = 0**, live check through the running app.
- Record each stage in `CHANGELOG.md` (what changed, bench before/after, what was checked by hand).
- No fixes via lists of specific words/names from the owner's documents — use morphology, checksums, context. Stop lists only for generic categories.
- Never commit real documents, `anon.db*`, `uploads/`, real names/addresses/requisites. Test data must be fictional, generated with `tests/bench/ids.py` (valid checksums).
- No force-push, history rewrite, deleting owner files or changing repo visibility without the owner's explicit "да".

## Architecture (v3)

### Pipeline
`anonymize_text_pipeline(text, db_path, session_id, use_spacy, use_llm)` in [core/anonymizer.py](core/anonymizer.py) builds `{original: "[TOKEN]"}` for a whole document. Passes in order (each sees earlier `[TOKEN]`s and skips them):

0. `_apply_global_known` — values remembered from earlier sessions (`known_entities`).
1. `_apply_opf_pass` — legal form + quoted name, incl. typographic quotes; only the name is masked. Paying bank in requisites is skipped (`detectors._is_payment_bank`).
2. `_apply_regex_pass` — rule detectors from [core/detectors.py](core/detectors.py) (checksums from [core/validators.py](core/validators.py), context windows, pymorphy3 grammemes for persons, address component chains; values only), then legacy `REGEX_PATTERNS`. **Patterns never cross a newline** (`[^\S\n]`, not `\s`). Positional replacement.
3. `_apply_spacy_pass` — spaCy PER/ORG, legal form stripped, filtered.
4. `core/llm.py: apply_llm_pass` — optional Ollama.
5. `_propagate_surnames` — a surname of a found person anywhere in the text → same token.
6. `_apply_known_entities` — re-applies session mappings (bounded).

Text-level API: `anonymize_text()` / `restore_text()` (used by the bench and tests).

### One token per entity, exact restore
`db.get_or_create_token` reuses the token of the same entity (`_same_entity_token` + [core/entities.py](core/entities.py): surname key in any case/gender, compatible name/initials; companies by word lemmas; numbers by digits). Several `mappings` rows may share a token (no unique index on token since schema v3, `PRAGMA user_version`).
While a file is anonymized, `make_finder(..., log)` records `(token, original)` in document order → `occurrences` table keyed by the output file name. Deanonymizing that file uses `make_rev_finder(..., occurrences)`: the k-th occurrence of a token gets the k-th recorded form. New text (an LLM reply) gets the first/main form; a hand-edited «Базовая форма» (`canonical_edited=1`) always wins. Token parsing is tolerant (`TOKEN_LOOSE_RE`: `FIO_1`, `[FIO 1]`, `[ФИО_1]`…).

### Format handlers
[core/handlers.py](core/handlers.py): `process_uploaded_file` → detect once on the whole document text → apply spans place by place → anonymized file name (`anonymize_filename`) → `leak_check` (re-extract output incl. metadata via [core/extract.py](core/extract.py), report originals still present and detector hits outside tokens).
- DOCX/PPTX: [core/ooxml.py](core/ooxml.py) on zip/XML level — every `w:p`/`a:p` in every part (text boxes, footnotes, comments, charts), revisions accepted, hyperlink targets (URL-decoded), metadata scrubbed. Replacement by char position across runs keeps formatting.
- RTF: [core/rtf.py](core/rtf.py) decodes `\'hh`/`\uN` with a char→raw-bytes map; only text bytes are rewritten.
- PDF: words with positions; lines of a block joined by space; redaction with token text sized to fit. Scans → OCR.
- Images and scanned pages: [core/ocr.py](core/ocr.py) (Apple Vision, macOS only), black boxes with token, EXIF dropped. Not reversible.
- DOC/ODT: converted to DOCX via `textutil` (macOS) or LibreOffice, then `ooxml.normalize_wordml` fixes textutil's non-standard markup (Word «unreadable content»).
- Replacement everywhere goes through `replace_spans`/`replace_bounded` (boundaries: letters for words, digits for numbers) — never `str.replace`.

### Paths, app, security
[app.py](app.py): `DATA_DIR` = repo dir in dev, `~/Library/Application Support/Anonymizer` (or `%APPDATA%`) when frozen, `ANONYMIZER_DATA_DIR` overrides. Uploads older than 30 days are purged at start. `main()`: free port, native window via pywebview (`--browser`, `--server` flags). `_local_only` rejects foreign Host/Origin/cross-site requests. Output names are recorded per input in `uploads/<sid>/manifest.json`. `/api/sessions/<sid>/preview/<file>` (text with tokens, forms, occurrences, leak check) and `/page/<file>/<n>.png` feed the in-app preview; a default session name is replaced by «<file> · date» after the first processing.
Build: `Anonymizer.spec` (onedir; Mac `.app` + Windows), `build_macos.sh` (icon → icns, PyInstaller, ad-hoc codesign, DMG).

### Version
Single source: [core/version.py](core/version.py), shown in the UI via `/api/status`.

### Frontend
Single file [static/index.html](static/index.html), no build step.

### Local data never in git
`Проверка распознавания текста/`, `private/` (audit output: `scripts/audit_folder.py`), `*.bundle`, `*.tar.gz`, `anon.db*`, `uploads/` are git-ignored. Test data must be fictional.
