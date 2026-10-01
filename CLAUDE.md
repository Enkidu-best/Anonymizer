# CLAUDE.md

Guidance for Claude Code in this repository. The development plan and working rules live in [CLAUDE_CODE_TASK.md](CLAUDE_CODE_TASK.md); detection rules per PII type live in [docs/PII_VARIANTS_CATALOG.md](docs/PII_VARIANTS_CATALOG.md). Read both before changing detection logic.

## Project

Local-only PII anonymizer/deanonymizer for Russian legal documents (152-FZ, banking and trade secrets). Flask backend + single-page frontend, packaged with PyInstaller. Formats: TXT, DOCX, PDF (text layer only), XLSX, RTF. The owner is a lawyer: talk to them in Russian, briefly, with results and what was verified.

Main metric is **recall**: a missed name leaks abroad, an extra mask only slightly hurts analysis.

## Commands

```bash
python3.12 -m venv .venv312 && .venv312/bin/pip install -r requirements-dev.txt   # Python 3.12
python app.py                          # opens http://127.0.0.1:5000
python -m pytest -q                    # unit + format tests (UI tests need `playwright install chromium`)
python tests/bench/run.py              # recall bench, 200 variants, regex only
python tests/bench/run.py --ner        # same with spaCy
# tests/test_holdout_contract.py — contract the rules were NOT tuned on (overfitting guard)
./build_macos.sh / build_windows.bat   # PyInstaller build
```

Debug endpoints: `/api/status`, `/api/debug`, `/api/llm-status`.

## Working rules (from CLAUDE_CODE_TASK.md §0)

- Test first: add a failing case to `tests/bench/corpus.py` or `tests/test_*.py`, then fix.
- After every change: pytest green, bench % not lower than the last CHANGELOG entry, **FP = 0, RT = 0**, live check through the running app.
- Record each stage in `CHANGELOG.md` (what changed, bench before/after, what was checked by hand).
- No fixes via lists of specific words/names from the owner's documents — use morphology, checksums, context. Stop lists only for generic categories.
- Never commit real documents, `anon.db*`, `uploads/`, real names/addresses/requisites. Test data must be fictional, generated with `tests/bench/ids.py` (valid checksums).
- No force-push, history rewrite, deleting owner files or changing repo visibility without the owner's explicit "да".

## Architecture (v2.3)

### Pipeline
`anonymize_text_pipeline(text, db_path, session_id, use_spacy, use_llm)` in [core/anonymizer.py](core/anonymizer.py) is the single entry point. Passes run in order; each sees text with earlier `[TOKEN]` placeholders already inserted and skips them (`TOKEN_INNER_RE`, `PARTIAL_TOKEN_RE`):

0. `_apply_global_known` — values remembered from earlier sessions (`known_entities`).
1. `_apply_opf_pass` — legal form + quoted name; only the name is masked (`ООО «X»` → `ООО «[YUL_1]»`).
2. `_apply_regex_pass` — first the rule detectors from [core/detectors.py](core/detectors.py) (form + context + checksums from [core/validators.py](core/validators.py); pymorphy3 grammemes Surn/Name/Patr for persons; address = chain of components; only values are marked, keywords stay), then the legacy `REGEX_PATTERNS`. Overlaps: first come wins. Replacement is positional.
3. `_apply_spacy_pass` — spaCy `ru_core_news_lg` PER/ORG, filtered by `_validate_spacy_fio` / `_validate_spacy_org`; names normalized with pymorphy3 (`_normalize_fio`).
4. `core/llm.py: apply_llm_pass` — optional Ollama call.
5. `_apply_known_entities` — re-applies mappings already in the session.

Each pass returns `(new_text, {original: "[TOKEN]"})`; the merged dict is what format handlers apply via `replace_bounded()` (one pass, longest first, word/number boundaries — never plain `str.replace`). Exclusions (`exclusions` table) are honoured by every pass. The paying bank in requisites («р/с … в ПАО Сбербанк») is deliberately not masked (`_is_payment_bank`).

NER loads on a background thread (`start_ner_loading`); UI polls `/api/status` and works regex-only meanwhile.

### Format handlers
[core/handlers.py](core/handlers.py): `process_uploaded_file` → `_anon_<fmt>` / `_deanon_<fmt>`. Handlers extract text, run the pipeline to build the replacement dict, then apply it element by element (DOCX paragraph incl. tables/headers/footers with run-aware `_replace_para`, XLSX string cells, PDF redaction rects, RTF raw text) to keep formatting. DOCX tracked changes are accepted first. Known gaps are listed in CLAUDE_CODE_TASK.md §2.3 (items 21-25).

### Tokens and DB
[core/db.py](core/db.py), SQLite. Tokens are ASCII `[FIO_1]`, `[YUL_2]`… (`_PREFIX` maps Russian entity types to prefixes; ASCII keeps PDF insertion safe). Numbering per session and prefix = `MAX+1`. `mappings` is UNIQUE on `(session_id, original_form, entity_type)`; `get_reverse_mappings` feeds `apply_reverse` and returns `original_form` (exact case) unless the user edited «Базовая форма» by hand (`canonical_edited=1`). Other tables: `sessions`, `exclusions`, `known_entities`, `user_patterns`.

### Paths
[app.py](app.py) splits `BUNDLE_DIR` (read-only, PyInstaller `_MEIPASS`) from `DATA_DIR` (writable; holds `anon.db`, `uploads/`). Never write to `BUNDLE_DIR`. Moving data to Application Support / %APPDATA% is planned (stage 8).

### Version
Single source: [core/version.py](core/version.py). Shown in the UI via `/api/status`.

### Frontend
Single file [static/index.html](static/index.html), no build step.
