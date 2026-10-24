# CLAUDE.md

Guidance for Claude Code in this repository. The development plan and working rules live in [CLAUDE_CODE_TASK.md](CLAUDE_CODE_TASK.md) (task 2 with owner-specific findings: `private/CLAUDE_CODE_TASK_2.md`, local only); detection rules per PII type live in [docs/PII_VARIANTS_CATALOG.md](docs/PII_VARIANTS_CATALOG.md). Read both before changing detection logic.

## Project

Local-only PII anonymizer/deanonymizer for Russian legal documents (152-FZ, banking and trade secrets). Flask backend (port 8070) + single-page frontend in a Chrome app window; `.app` by the shared builder. Formats: TXT, DOCX, PDF (text layer only), XLSX, RTF. The owner is a lawyer: talk to them in Russian, briefly, with results and what was verified.

Main metric is **recall**: a missed name leaks abroad, an extra mask only slightly hurts analysis.

## Commands

```bash
python3.12 -m venv .venv312 && .venv312/bin/pip install -r requirements-dev.txt   # Python 3.12
./start.sh                             # server 127.0.0.1:8070 + Chrome app window; NO_BROWSER=1 for checks
python app.py                          # server only (ANONYMIZER_PORT; tests 8074)
python -m pytest -q -m "not ui"        # unit + format tests
python -m pytest -q -m ui              # window scenarios UC-… of docs/Функциональное описание.md (Playwright)
python scripts/ui_check.py             # contrast (common/a11y), RU/EN alphabet, cut text, dead buttons - every screen
bash scripts/check_all.sh [--ui]       # before push (pre-push hook)
python tests/bench/run.py              # recall bench, 200 variants, regex only
python tests/bench/run.py --ner        # same with spaCy
# tests/test_holdout_contract.py — contract the rules were NOT tuned on (overfitting guard)
./build_macos.sh                       # Anonymizer.app by the shared builder (common/app); Windows bat/spec - 3.x, untouched
python scripts/audit_folder.py <dir>   # local audit of real documents → private/ (git-ignored)
python scripts/e2e_private.py [--llm]  # END-TO-END on the owner's documents via the HTTP API — run before saying «done»
python -m pytest tests/e2e             # the same scenario on generated fixtures with a ground truth
```

Debug endpoints: `/api/status`, `/api/debug`, `/api/llm-status`.

## Working rules (from CLAUDE_CODE_TASK.md §0 and task 2 §0)

- **Check yourself as a user**: the end-to-end test (`tests/e2e/runner.py`) processes, downloads, re-reads the file with INDEPENDENT code (`tests/e2e/independent.py`, never core.ooxml/rtf), restores, simulates an LLM reply, edits mappings, greps the journal. Before claiming a fix: pytest + bench + `scripts/e2e_private.py` with 0 errors. When adding a check, prove it catches the bug (inject the fault once, see it fail).

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
7. `consolidate` — `db.consolidate_session`: tokens that are one entity by the automatic merge rules (`entities.auto_groups`: numbers by digits, persons by pairwise-compatible forms, companies without legal form/quotes or by lemmas, addresses by `address_parts`) are merged into the lowest token. Pairs for a human: `entities.review_pairs` («Проверка дублей»; «Это разные» → `distinct_pairs`). `_record_org_forms` stores each company's legal form (`mappings.opf`).

Text-level API: `anonymize_text()` / `restore_text()` (used by the bench and tests).

### One token per entity, exact restore
`db.get_or_create_token` reuses the token of the same entity (`_same_entity_token` + [core/entities.py](core/entities.py): person = surname key + compatible name/initials; company = word lemmas or compact letters-only key for OCR splits; address = `address_parts`: abbreviations, settlement kind, district, word order before the house; numbers = digits + series letters, never by similarity). Several `mappings` rows may share a token.
**Mappings are never deleted** (schema v4, `status`: active / excluded = «не маскировать» / replaced = edited); inactive rows still restore earlier files; token numbers are never reused; merges keep the old token in `token_aliases`. Exclusions (`db.ExclusionSet`) match every form of an excluded entity.
**Restore by place**: while anonymizing, `make_finder(..., places=)` stores per paragraph/cell a fingerprint of its anonymized text + `[(token, original)]` (`places` table). `make_rev_finder` gives an unchanged place its exact forms regardless of order/file; changed text (an LLM reply): persons are declined by [core/cases.py](core/cases.py) (`target_case` from the left word, apposition, coordination; `decline` by endings; undecided places → one short LLM request in `find.prepare`, only the case is taken) and counted in `check_case`; companies get «» and the legal form when standing alone (`org_in_new_text`, [core/settings.py](core/settings.py)). Values come back as `Restored` with `.mark` (exact / case): yellow highlight of «case» places in Word (own run, copied `w:rPr`) and the coloured restore preview (`restore_marks/`). The value goes into the run holding most of the token. Unknown tokens are reported (`result['restore']`). Token parsing is tolerant (`TOKEN_LOOSE_RE`).

### Lexicon and the LLM
[core/lexicon.py](core/lexicon.py): roles of parties, positions, headings, public bodies, «all words ordinary» — never masked by any layer (manual additions excepted). LLM dates only with birth context.
4.0: one mode «Обезличить». Setting `use_llm` («Использовать нейросеть», off by default) adds [core/llm_review.py](core/llm_review.py) in a background thread after the rules (`app._start_review`, `/api/llm-jobs/<id>`, `/apply`; «Снять маску» unchecked in the window) and the LLM case choice on restore (`find.prepare` is gated by the same setting). `llm_only` is only `scripts/llm_only_eval.py` (quality measurements).

### Dictionary, sessions, migration (4.0)
«Словарь»: «Всегда маскировать» = `known_entities` (`source`: manual / unmask / legacy), «Никогда не маскировать» = `never_mask` (added to `get_exclusions` of every session). Values go there only when the user chooses «всегда». Sessions: name «Сессия ДД.ММ» (`db.default_session_name`), deleted whole by `purge_expired` (retention 7/30/90) or the button - no bin (152-FZ data minimization). Files of a session - `core/session_files.py` (`manifest.json` v2: section anon/restore, result). First start of 4.0: `core/migrate.py` (backup `backup-before-4.0/`, counts check, rollback).

### Journal
[core/log.py](core/log.py): `~/Library/Logs/Anonymizer` (also from the sources); events, stages with durations, counts per type, LLM metrics, job JSON with heartbeat; never values or file names. Window: Настройки → Служебное: open folder, «Последние задачи», diagnostic mode (with data, off at restart).

### Format handlers
[core/handlers.py](core/handlers.py): `process_uploaded_file` → detect once on the whole document text → apply spans place by place → anonymized file name (`anonymize_filename`) → `leak_check` (re-extract output incl. metadata via [core/extract.py](core/extract.py), report originals still present and detector hits outside tokens).
- DOCX/PPTX: [core/ooxml.py](core/ooxml.py) on zip/XML level — every `w:p`/`a:p` in every part (text boxes, footnotes, comments, charts), revisions accepted, hyperlink targets (URL-decoded), metadata scrubbed. Replacement by char position across runs keeps formatting.
- RTF: [core/rtf.py](core/rtf.py) decodes `\'hh`/`\uN` with a char→raw-bytes map; only text bytes are rewritten.
- PDF: words with positions; lines of a block joined by space; redaction with token text sized to fit. Scans → OCR.
- Images and scanned pages: [core/ocr.py](core/ocr.py) (Apple Vision, macOS only), black boxes with token, EXIF dropped. Not reversible.
- PDF with a readable text layer and no big page images (`pdf_is_digital`) → DOCX via pdf2docx (`_pdf_to_docx`, `_unglue_line_ends` puts back spaces pdf2docx drops), processed as DOCX; setting `keep_pdf` adds the anonymized PDF. Scans stay PDF.
- A value found whole in one place but cut by a paragraph break elsewhere is added by `_broken_occurrences` and masked part by part.
- DOC/ODT: converted to DOCX via `textutil` (macOS) or LibreOffice, then `ooxml.normalize_wordml` fixes textutil's non-standard markup (Word «unreadable content»).
- Replacement everywhere goes through `replace_spans`/`replace_bounded` (boundaries: letters for words, digits for numbers) — never `str.replace`. A value across a paragraph break is split into lines with the same token (`handlers._split_lines`); after writing, `_verify_written` re-reads the file — a value still present is `apply_errors`, never a silent skip.
- PDF pages with a broken OCR text layer (`text_layer_ok`) are cleaned and re-OCRed; OCR text is assembled by `ocr.layout` (columns, rows, wrapped lines).

### Paths, app, security
[app.py](app.py): `DATA_DIR` = platformdirs (`~/Library/Application Support/Anonymizer`) also from the sources, `ANONYMIZER_DATA_DIR` overrides; journal `~/Library/Logs/Anonymizer/` (`ANONYMIZER_LOG_DIR`). Fixed port 8070 (`ANONYMIZER_PORT`), `/api/info` - version for the window check. The window: Chrome `--app` opened by `desktop/launcher.sh` (from `Anonymizer.app`) or `start.sh`. One file per request: upload `POST /api/sessions/<sid>/files`, process `POST /api/sessions/<sid>/process` (progress `/api/progress/<id>`); `/api/process` (several files) stays for scripts and the e2e test. `_local_only` rejects foreign Host/Origin/cross-site requests. Output names, section and result per input - `uploads/<sid>/manifest.json` (`core/session_files.py`); replies for «Вернуть данные» - `uploads/<sid>/replies/`. `/api/sessions/<sid>/preview/<file>` (text with tokens, forms, occurrences, leak check) and `/page/<file>/<n>.png` feed the in-app preview. Session names never come from file names.
Build: `build_macos.sh` → shared builder `common/app` (C launcher + `desktop/launcher.sh`). `Anonymizer.spec` / `build_windows.bat` - Windows 3.x, not maintained (owner's decision 24.10).

### Version
Single source: [core/version.py](core/version.py), shown in the UI via `/api/status`.

### Frontend
[static/index.html](static/index.html) + [static/app.css](static/app.css) (LKD tokens, light/dark) + `static/js/` (core, anon, preview, restore, lists), strings only from `static/i18n/ru.json` / `en.json`, no build step. Description: `docs/Функциональное описание.md` v2.1, mockups `docs/макеты 01/`.

### Local data never in git
`Проверка распознавания текста/`, `private/` (audit output: `scripts/audit_folder.py`), `*.bundle`, `*.tar.gz`, `anon.db*`, `uploads/` are git-ignored. Test data must be fictional.
