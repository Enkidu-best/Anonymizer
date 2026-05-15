# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Anonymizer is a **local-only** Russian-language document PII redaction tool (ФЗ-152 compliance). It detects and replaces sensitive entities with `[TYPE_N]` tokens (e.g., `[FIO_1]`, `[INN_2]`) and supports deanonymization (token → original value). No internet required; all processing is on-device.

## Development Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run dev server (opens browser automatically at http://127.0.0.1:5000)
python app.py

# Build Windows executable
build_windows.bat

# Build macOS executable
./build_macos.sh
```

Requires **Python 3.11–3.12** — numpy 1.x is incompatible with Python 3.13+.

On first run, Natasha NER model downloads ~220 MB to `~/.natasha` in a background thread. The server is immediately usable for structured-data (INN, phone, etc.) processing before NER finishes loading.

## Architecture

### Processing Pipeline (`core/anonymizer.py`)

Four sequential passes, each operating on non-overlapping spans:

1. **OPF Regex** — Russian legal entity forms (ООО, АО, ПАО, etc.) with full case inflections; extracts org names from quoted patterns
2. **Structured Regex** — INN (10/12-digit), OGRN, КПП, bank accounts, БИК, СНИЛС, passports, phones, emails, SWIFT, addresses
3. **Natasha NER** — PER/ORG entity recognition loaded asynchronously at startup; FIO candidates pass a 3-layer validation filter (initials pattern → patronymic suffix → pymorphy2 morphtags)
4. **LLM via Ollama** (optional) — Document-level extraction using local model (mistral-nemo, qwen2.5, etc.); one call per document to avoid timeout multiplication

Main entry: `anonymize_text_sequential(text, db_path, session_id, use_llm, use_regex_ner)`

### Session & Storage (`core/db.py`)

SQLite (`anon.db`, WAL mode). Two tables:
- `sessions` — UUID id, name, created_at
- `mappings` — `(session_id, canonical_form, entity_type)` UNIQUE; token auto-increments per type within a session

`get_or_create_token()` enforces idempotency — same entity always gets the same token within a session.

### Format Handlers (`core/handlers.py`)

Dispatches by file extension: TXT, DOCX, PDF, XLSX, RTF.

- **DOCX**: XML-level tracked-change acceptance before processing; per-run and cross-run replacement; iterates body, tables, headers, footers
- **PDF**: Always converted to DOCX via `pdf2docx` for anonymization (preserves deanon fidelity); falls back to `.txt` if system fonts are unavailable. Scanned PDFs are rejected (no OCR).
- **XLSX**: Cell-level replacement with formatting preservation
- **RTF**: Raw string replacement (complex RTFs may lose formatting)

### LLM Client (`core/llm.py`)

Connects to Ollama at `localhost:11434`. `try_start_ollama()` auto-spawns the daemon on Windows/macOS/Linux. Text is chunked at 3000 chars, temperature=0, 120 s timeout. Progress tracked in a global `_llm_progress` dict, polled by frontend via `GET /api/llm-progress`.

### Web App (`app.py` + `static/index.html`)

Flask server on `127.0.0.1:5000`. `static/index.html` is a single-page app with embedded CSS and JS (no build step, no bundler). Key API groups:
- `POST /api/process` — upload + anonymize/deanonymize (multipart); returns results + mappings
- `GET|POST|DELETE /api/sessions` and `/api/sessions/<sid>/mappings` — session/mapping CRUD
- `GET /api/status`, `POST /api/ner-retry` — NER lifecycle
- `GET /api/llm-status`, `POST /api/llm-start`, `POST /api/llm-model` — Ollama control
- `GET /api/download/<sid>/<file>`, `GET /api/download-all/<sid>` — file retrieval

## Key Implementation Details

- **Token format**: `[TYPE_N]` where TYPE is an ASCII prefix (FIO, YUL, INN, OGRN, KPP, RS, KS, BIK, SNILS, PASSPORT, TEL, EMAIL, SWIFT, ADR, DOB, LIC, URL). ASCII-safe for binary formats.
- **FIO normalization**: Natasha normalizes to nominative case; deanonymized FIOs may differ from original inflected forms — this is a known limitation.
- **pkg_resources polyfill**: `core/anonymizer.py` replaces `pkg_resources` for pymorphy2 under PyInstaller + Python 3.12+ (setuptools entry_points failure workaround).
- **PyInstaller build**: Hidden imports explicitly listed in `build_windows.bat`/`build_macos.sh` and `Anonymizer.spec`; `static/` and `core/` embedded via `--add-data`. The app detects frozen state via `sys.frozen` to resolve paths correctly.
- **Uploads directory**: `uploads/<session_id>/input/` and `uploads/<session_id>/output/` created per session; deleted on session deletion.
