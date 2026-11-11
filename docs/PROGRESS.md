# Progress

## Phase 0 - Scaffold: DONE (2026-11-11)

**Delivered**
- Services: `gateway` (8000), `library` (8001), `knowledge` (8002), `mock-eqms` (8003), `workflow` (8004) as FastAPI apps; `web` (5173) as React + Vite. No Docker.
- Each service has its own `.venv` (gitignored) and `requirements.txt`; `web` has `package.json`.
- `GET /health` on every service returns `{"service": "<name>", "status": "ok"}` (web serves it via a Vite dev middleware).
- Closed-loop rule enforced by `tests/test_closed_loop.py`: scans source (AST imports) and dependency files of every non-gateway service for networking libraries; also checks `web` for network packages and `fetch`/XHR/WebSocket calls. Verified it fails when `import requests` is injected.
- `gateway/app/allowlist.py`: `ALLOWED_HOSTS` (empty) + `GATEWAY_ALLOWLIST` env; `check_url()` raises `UrlNotAllowed`; `safe_get()` checks before importing/using httpx. Test proves rejection happens before any network call.
- `.env.example`, `scripts/` (`up.ps1`, `down.ps1`, `test.ps1`, `seed.ps1` stub, shared `common.ps1`).

**Decisions**
- Non-gateway health tests use raw ASGI (stdlib) instead of `TestClient`, which needs `httpx`.
- `up.ps1` launches each service with `Start-Process` (hidden, detached, logs in `.run/logs/`), writes launcher PIDs to `.run/pids.json` immediately, then polls each `/health`. `down.ps1` reads only `pids.json` and runs `taskkill /T /F` per PID (kills uvicorn/node children too).
- Original `up.ps1` used PowerShell jobs + netstat to find PIDs. Both were wrong: jobs die with the launching shell, and in this environment `netstat -ano` and `Get-NetTCPConnection` do not list the services' 127.0.0.1 listeners at all (services answered HTTP while netstat showed none), so `pids.json` was written empty. Cause confirmed by observation; the exact reason netstat is blind here was not isolated. PIDs now come from `Start-Process -PassThru`, no netstat.
- Phase 0 `web` makes no network calls at all (enforced by test).

**Test status:** `scripts\test.ps1` - all passing (gateway 4, library/knowledge/mock-eqms/workflow 1 each, closed-loop 4, web 1).

**Verified (2026-11-11):** `up.ps1` in one shell -> all six `/health` 200 from a separate fresh shell -> `down.ps1` from that fresh shell -> all six ports refuse connections, all six PIDs dead, `pids.json` removed, no leftover uvicorn/vite processes. `up.ps1` refuses to run if `pids.json` already exists.
**Caveat:** port-free proof uses HTTP connection refusal plus PID checks, because netstat can't see these listeners in this environment. `scripts\test.ps1` was not re-run after the script change (tests don't touch the scripts).

## Phase 1 - Regulatory Source Library + Source Registry: DONE (2026-11-11)

**Delivered**
- `gateway/app/registry.py`: source CRUD (url, jurisdiction, product/document types, check frequency, legal weight, auto-accept). Add / edit / retire are *proposals*; a different steward or QA must approve before they take effect. Proposer approving own change is refused; viewers cannot propose or approve. Proposal URLs are checked against the allow-list.
- `gateway/app/audit.py`: append-only, hash-chained audit log in the same transaction as each change. SQLite triggers block UPDATE/DELETE; `verify_chain()` detects tampering done around them. Fetches (ok / refused / failed) are logged too.
- `library/app/store.py`: versioned items (source url, retrieved_at, document_date, version/docket, status draft|final|withdrawn|superseded, SHA-256 checksum, legal weight regulation|guidance|standard). Unchanged checksum = no-op. New/changed content waits in the steward queue; accept makes it current and marks the old one `superseded`; reject keeps it in history. Redline = unified diff + structured add/del/ctx lines. Per-source `auto_accept` (set through the two-person edit flow) accepts automatically and is recorded as `auto-accept-rule`. Stale items cannot regress the current version.
- `gateway/app/fetcher.py`: explicit-trigger fetch only (no scheduler). Allow-list is now `www.ecfr.gov`, `www.federalregister.gov`. eCFR adapter (date resolved from `titles.json`; the API rejects `current` and requires gzip) and Federal Register adapter. Fixed FR document: **2018-26957** (FDA "Data Integrity and Compliance With Drug CGMP: Questions and Answers", docket FDA-2018-D-3984). **Federal Register items are METADATA-ONLY - see Known limitations.**
- Endpoints. gateway (8000): `GET/POST /sources`, `PATCH/DELETE /sources/{id}` (propose edit/retire), `POST /seed-defaults`, `GET /changes`, `POST /changes/{id}/approve|reject`, `POST /sources/{id}/fetch`, `GET /audit`. library (8001): `POST /internal/ingest`, `GET /queue`, `GET /queue/{id}`, `GET /queue/{id}/redline`, `POST /queue/{id}/accept|reject`, `GET /library`, `GET /library/versions`.
- CLAUDE.md: added a **Hard rules** section (no ISO / licensed standard content; closed loop). Test `test_no_iso_or_licensed_standard_content` scans the repo.

**Decisions**
- Library cannot make HTTP calls (Phase 0 rule), so the gateway pushes fetched content to `POST <LIBRARY_URL>/internal/ingest` via `allowlist.internal_post`. That path accepts only `<LIBRARY_URL>/internal/...`; `127.0.0.1` is NOT on the external allow-list.
- Queue accept/reject lives on the library (8001), not proxied through the gateway; decisions are stored on the item row (decided_by/at/note), not in the gateway audit log.
- Users are simulated: `X-User` header against a fixed table (`steward-a`, `steward-b`, `qa-1`, `viewer-1`). Not real authentication. The library has its own copy of the reviewer names.
- Storage: stdlib SQLite under `<service>/data/` (gitignored). Library routes are tested with raw ASGI (no httpx there).
- The real-network test runs the library as a subprocess from its own venv, so the two `app` packages never share a process.

**Test status:** `scripts\test.ps1` - all passing (gateway 33, library 18, knowledge/mock-eqms/workflow 1 each, root 8, web 1). Two real-network tests (`-m network`, skip if offline) run live through gateway -> library: eCFR 21 CFR 211.22 (full text) and Federal Register 2018-26957 (metadata-only).

**Not verified / caveats**
- The offline skip path of the network tests was not exercised (online here).
- `up.ps1` was not re-run after Phase 1 changes; `seed.ps1` is still a stub (use `POST /seed-defaults`).
- No web UI for the queue/redline yet.

## Known limitations (read before calling these bugs)

- **Federal Register items are metadata-only. This is deliberate.** `federalregister.gov` serves its full-text pages (`.txt`, `.xml`, `.html`) behind bot protection: they answer `302 -> unblock.federalregister.gov`. Only the JSON API (`/api/v1/documents/<number>.json`) responds normally. We do not follow redirects (the allow-list would block the unblock host anyway) and do not try to evade the protection. For FR sources the library stores: title, publication date, document number, docket, abstract, source URL.
- **How it is marked:** library items have `content_kind` = `full_text` | `metadata_only`. eCFR items are `full_text`; FR items are `metadata_only`, and their stored text ends with the line `Full text: NOT STORED (metadata-only record)`. Anything downstream (knowledge, workflow, UI) must check `content_kind` before treating an item's text as the full document - a metadata-only item is not enough to answer clause-level questions.
- **Re-checking later:** if FR full text becomes available through the API or another allow-listed route, change `fetch_federal_register` in `gateway/app/fetcher.py`; the next fetch would then arrive as a changed item in the steward queue with a redline.
- Existing library DBs created before `content_kind` existed are migrated on startup (column added, default `full_text`).

## Phase 2 - knowledge + mock-eqms (synthetic data): DONE (2026-11-11)

**Delivered**
- **Synthetic data, all fictional ("Examplon Pharma")**, labelled `synthetic: true` + a SYNTHETIC / FICTIONAL banner in every file; a test fails if any file lacks it or names a real company.
  - `knowledge/data/sops/`: 7 SOPs (SOP-DEV-001 deviation mgmt, DEV-002 classification, INV-001 root cause, CAPA-001, CAPA-002 effectiveness, CHG-001 change control, CHG-002 impact) in 10 files: a superseded DEV-001 v2.0, a draft CAPA-001 v5.0 and an approved-but-future-dated (2027-03-01) CHG-001 v3.0 exist to exercise the effective logic.
  - `knowledge/data/templates/deviation_template.json` (11 required fields) and `mock-eqms/data/deviation_form.json` (same fields; a root test keeps them identical).
  - `mock-eqms/data/deviations_seed.json`: 20 closed deviation records (9+ event types, minor/major/critical, 3 fictional sites, root cause, CAPA outcome incl. 6 "correction only, no CAPA").
  - `knowledge/data/traceability.json`: hand-authored requirement -> SOP-section map (211.100, 211.192, 211.22, 211.180, 211.160, 211.68, 211.188; public 21 CFR headings only).
- **mock-eqms (8003)**: SQLite store seeded idempotently. `record_kind` = `template` | `record`; record status `draft|open|closed`. Read-only router `GET /read/records?type=&severity=&status=&site=`, `/read/records/{id}`, `/read/templates` (test asserts GET-only; blank templates never appear as records). Write: `POST /write/records` returns 409 unless `human_confirmed=true` and `confirmed_by` is set; validates required fields/enums against the form; stores as `draft` (id `SYN-DEV-DRAFT-nnnn`); caller cannot set status, closure or CAPA.
- **knowledge (8002)**: loads SOPs + template from flat files, validates at load (header fields, synthetic label, sections, no duplicate versions, at most one effective version per SOP, traceability targets exist). `effective` = status `effective` AND effective_date reached (injectable clock; `KNOWLEDGE_TODAY` env override). Defaults to effective versions only (`include_all` / `include_non_effective` to see the rest, flagged). Keyword search (tf-idf, section level, snippets; no embeddings). `GET /documents`, `/documents/{no}[?version=]`, `/documents/{no}/versions`, `/templates`, `/records`, `/search`, `/traceability[/{req}]` (mapping to a SOP with no effective version is flagged `stale`), `POST /internal/index-records` (refuses blank templates).
- **Gateway sync**: knowledge and mock-eqms cannot call each other, so `POST /sync/eqms-records` (steward/QA, explicit, audited) reads `mock-eqms /read/records` and pushes to `knowledge /internal/index-records`. Internal routing generalised to a fixed table (library POST `/internal/`, knowledge POST `/internal/`, mock-eqms GET `/read/` only); the eQMS write side is unreachable from the gateway; `127.0.0.1` is still not on the external allow-list.
- Tests: closed-loop guard extended (knowledge/mock-eqms may list only fastapi, uvicorn, pytest; no network imports; verified it fails when `httpx`/`import requests` are injected); root consistency tests; real-services integration test (mock-eqms -> gateway -> knowledge over localhost, no internet).

**Decisions / notes**
- URS alignment: see "Phase 2 URS alignment" below (done 2026-11-11 against the real URS text).
- `.gitignore` changed from `data/` to `*.db` so the committed synthetic data folders are not ignored (the old rule would have hidden them).
- IK-06 (confirmed by real URS) is implemented as: blank templates (`record_kind=template`) vs executed/filled-in records (`record_kind=record`, any status). Precedent retrieval later should filter to `status=closed`.
- IK-09 is **Should** priority (confirmed in the real URS): the write-with-confirmation endpoint exists and works but is not go-live-blocking if deferred. Writes are real in the mock only; nothing is ever auto-submitted.
- The `GET /sources`-style audit of gateway actions covers the sync; mock-eqms writes are not in the gateway audit log (the UI/caller talks to mock-eqms directly in Phase 2).

**Test status:** `scripts\test.ps1` - all passing (gateway 47, library 18, knowledge 27, mock-eqms 12, workflow 1, root 15, web 1).

**Not verified**
- `up.ps1`/`down.ps1` not re-run after Phase 2 (knowledge/mock-eqms start as before; their data dirs are now read at startup).
- Keyword search quality is only tested on the synthetic corpus (no relevance tuning; semantic search is Phase 3).

## Phase 2 URS alignment (2026-11-11)

The real URS text replaced my earlier interpretation. Only what differed was changed.

**Changed**
- Template files (`knowledge/data/templates/deviation_template.json`, `mock-eqms/data/deviation_form.json`, kept identical): `urs` tags per field now follow the real GW text; `urs_status: aligned` replaces the "ASSUMED" note; new `gw01_minimal_fields`; labels "Product or material" (was Product) and "Date discovered" (was Date detected) to match GW-01 wording.
  - GW-01 minimal initiator fields = event_type (record type), description, site, product, lot_number, equipment_id, detected_date.
  - GW-02 (system proposes, initiator confirms) = event_type, severity (preliminary classification).
  - GW-03 (required by the SOP/form, so questions get generated for them, not part of the initiator's minimal entry) = event_date, immediate_action, reported_by.
- **IK-05 gap fixed:** executed records had no version. Seed records now carry `version: "1.0"`; eQMS drafts get `0.1` (caller cannot set it); knowledge refuses unversioned records and returns `citation` = `<record id> v<version>` on records and search hits. Versions were already shown on SOP documents, templates and SOP search hits (IK-01).
- Tests: template tagging locked to the URS, no "ASSUMED" text left in data files, versioned records.

**Already matched (unchanged):** IK-01 effective-only by default with version shown; IK-06 templates vs executed records; IK-09 Should-priority write-after-human-confirmation, never submits/approves/closes; IK-02 read-only eQMS connection.

**URS items NOT met yet (not Phase 2 scope or not built - do not mistake for done)**
- IK-02: the mock eQMS holds only deviations (CAPA appears only as an outcome inside a deviation). No separate CAPA, change-control, nonconformance, audit or complaint records.
- IK-03: no LIMS, MES/EBR, ERP, CMMS, LMS or supplier-system connectors.
- IK-04: no least-privilege service accounts and no per-user result filtering (the read API returns everything to every caller).
- IK-07: the traceability map is hand-authored and static. There is no "suggest links, procedure owner confirms" workflow and no confirmation status on a link.
- IK-08: SOPs are read from flat files at service startup. There is no 24-hour re-index (target is still "to confirm" in the URS) and no reload trigger; a revised file is picked up only on restart. Superseded versions are not used by default (that part works).
- IK-01 scope: only SOPs and the deviation template are indexed - no policies or work instructions.
- GW-01..GW-05 describe the drafting workflow (record-type suggestion, proposals with reasoning and citations, generated questions, "unknown"/evidence/assignment, completeness checklist). None of that is built; Phase 2 provides only the data (template, required fields, SOPs, precedent records, traceability) it will need. Question generation will also need each form field linked to the SOP section/requirement it satisfies (not yet in the template).

## Phase 3 - retrieval, local LLM client, citation verifier: DONE (2026-11-11)

**Delivered**
- **Architecture (closed loop kept):** `workflow` cannot hold an HTTP client (Phase 0 rule, enforced by test), and "no other service may call Ollama" - so the **gateway owns every call, including Ollama**, and `workflow` is a *passive compute service*: `POST /internal/build-prompt`, `/internal/parse`, `/internal/verify`. Flow, all in `gateway/app/assist.py`: retrieve -> workflow builds prompt -> gateway calls Ollama -> workflow parses -> gateway fetches every *cited* source from the real stores -> workflow verifies -> gateway logs. Entry point `POST /assist/ask` (steward/QA, explicit, nothing scheduled).
- **Ollama = internal service** (`http://127.0.0.1:11434`, env `OLLAMA_URL`): added to the gateway's internal route table as `POST /api/chat` and `GET /api/tags` only; never on the external allow-list (tests assert `127.0.0.1`/`localhost` fail `check_url`/`safe_get`). `tests/test_closed_loop.py` now also fails if any other service mentions the Ollama endpoint, and asserts workflow has no networking imports and only fastapi/uvicorn/pytest dependencies. The internal table is now `(path prefix, methods)` rules per service (library, knowledge, mock-eqms, workflow, ollama); the eQMS write side remains unreachable.
- **Retrieval (keyword only, no embeddings):** library items (`GET /search` new), SOP sections, closed past deviations (draft/open excluded), traceability entries, and suggested requirement links for the SOP sections found; a requirement-ID query (`211.192`, `21 CFR 211.192`) goes traceability -> mapped SOP sections + library. Library gained `/search`, `/library/{id}` (accepted items only: pending/rejected content is never citable) and `/library/version` (fingerprint of the current library); knowledge gained `include_text` on search and `/records/{id}`.
- **LLM client** (`gateway/app/llm.py`): Ollama `/api/chat` with JSON-schema structured output, temperature 0, fixed seed; model digest read from `/api/tags` and logged. Clear errors for model-not-installed (503 with the `ollama pull` command), Ollama down (502).
- **AI-06 config under change control:** model, system prompt, retrieval settings, generation settings and verifier thresholds are ONE versioned file `gateway/config/ai_config.json` (v1.0.0, model `qwen2.5:14b-instruct`) with an approval lock `ai_config.lock.json` (canonical SHA-256 + two distinct steward/QA approvers + change ref). Editing the file without `python -m app.ai_config approve --by <a> <b> --change-ref <ref>` makes the gateway refuse every AI call (503, logged). There is deliberately **no environment override** of the model (it would bypass change control). Change control itself is simulated (a lock file), like the rest of the demo.
- **Citation verifier** (`workflow/app/verifier.py`, stdlib only): per citation checks (1) source exists at the cited version, (2) locator exists, (3) quote appears verbatim (whitespace/case tolerant, `...` fragments), (4) lexical support score (share of the statement's content words in the passage; thresholds 0.60 verified / 0.35 low, number check, negation check vs the quote). Verdicts: `verified | low_confidence | mismatch | invalid_citation | unsupported`. Superseded / draft / not-yet-effective sources and metadata-only items can never reach `verified`. One bad citation flags the whole statement. Nothing is dropped or silently passed; no citation = `unsupported`. Each verified citation carries a link to the passage.
- **AI-02 behaviour:** if retrieval finds nothing the model is **not called** and the answer says no support was found; if the model says so, or no statement verifies, the response says so (`no_support_found`, `notice`). Unparseable model output is reported, never presented as supported. A store outage while checking citations is a 502, not a "fabricated" verdict.
- **DI-03 logging:** every call appends a hash-chained audit entry (`ai.call`, same log as Phase 1/2): input, full prompt, retrieved source refs (`id@version`), model name + digest, config version + SHA-256 + approvers + generation/retrieval/verifier settings + system prompt, library version, knowledge snapshot, raw output, parsed output, per-source resolution, verified output, user, time. Failures/refusals are logged (`ai.call_failed`, `ai.call_refused`). What the user did is appended with `POST /assist/{call_id}/disposition` (`accepted | edited | rejected`, edited text required for edits, records how many statements were still flagged); the original entry is never changed.

**CITATION VERIFIER NUMBERS (positive class = "verified")**

| Set | Cases | TP | FP | FN | Precision | Recall |
|---|---|---|---|---|---|---|
| Main (30: 15 supported, 15 bad) | 30 | 15 | 0 | 0 | **1.000** | **1.000** |
| Stress (12 hard: 6 heavy paraphrases, 6 fluent near-misses) | 12 | 2 | 6 | 4 | **0.250** | **0.333** |

- Main set: all 15 bad citations caught (9 structural - missing source/version/section, fabricated quote, template-as-record, no citation, nonexistent library item, missing version, false mapping - plus unrelated statement, wrong number, superseded version, draft version, overreach, reversed meaning containing "not"). Two valid cases passed only barely (scores 0.625 and exactly 0.600 vs the 0.60 threshold).
- **Read the main number with caution:** I wrote both the cases and the verifier; the cases were designed knowing the method; only 30 cases. It shows the *structural* guarantees work, not real-world accuracy.
- **Stress set is the honest limit:** the verifier **verified all 6 fluent near-misses** (e.g. "only after 24 hours" vs "within 24 hours"; "30 working days" for critical when the passage says critical 15 / major 30; "prospective" for "retrospective"; swapped actors/categories) and missed 4 of 6 heavy paraphrases (low word overlap). **Lexical overlap cannot see meaning.** The verifier was NOT tuned against the stress set (that would be overfitting to a set I just wrote).
- **What this means for Phase 4:** "verified" = *a real source, a real passage, a verbatim quote, and the statement's words mostly appear there* - it does **not** mean the statement is correct. Structural fabrication (invented sources/sections/quotes, missing citations) is reliably blocked (hard test: those categories can never pass). Meaning-level errors are not reliably blocked. Phase 4 must therefore keep a human confirmation step and show the cited quote next to every statement. Recommended next step (not done): add an entailment check - e.g. a second constrained call to the local model ("does this passage support this statement? yes/no + reason"), or embeddings as an extra signal - and re-measure on exactly these two sets. Reproduce: `python tests/test_citation_verifier_eval.py`.

**URS alignment (real lines received)**
- AI-01: citations carry document (`source_id`), version, section/locator, a verbatim quote and a **link to the passage**; version is mandatory (except for traceability-map links, which are unversioned); uncited statements are flagged `unsupported`, never passed.
- AI-02: no support -> the system says so; citations are never invented (checked against the stores, existence + support); the model isn't even called when retrieval is empty.
- AI-06: model/system prompt/retrieval/generation settings are one version-controlled item with a change-control lock (simulated).
- DI-03: all required fields are logged (see above), including library version and the user's accepted/edited/rejected action. "Library version" = fingerprint of the current accepted library (`lib-<n>-<hash>`), not a human release number.

**Notes / decisions**
- Ollama is running locally (v0.40.16) but **no model was installed** when this was built; you are pulling `qwen2.5:14b-instruct`. Everything was therefore tested with a stand-in Ollama (a stdlib HTTP server returning scripted output) behind REAL library/knowledge/mock-eqms/workflow services over localhost. **No real model output has been seen yet.** Once the model is installed, run `gateway\.venv\Scripts\python.exe -m pytest -q --rootdir . tests -m llm -s` (the live test checks plumbing and schema, not answer quality).
- Structured output relies on Ollama's JSON-schema `format`; a small model may still produce unparseable or badly cited output - that is handled (reported, flagged), not assumed away.
- Embeddings deferred on purpose (needs a second model, an index to keep fresh, and a non-deterministic signal; keyword search is adequate for 7 SOPs / 20 records / a few library items). Cost: weaker paraphrase retrieval and a lexical-only verifier - see the stress numbers.
- `.env.example` gained `OLLAMA_URL` and `WORKFLOW_URL`. The model name is intentionally NOT an env var.
- Library items from the Federal Register are metadata-only; citing them is flagged `source_is_metadata_only` and cannot reach `verified`.

**Test status:** `scripts\test.ps1` - all passing (gateway 83, library 22, knowledge 32, mock-eqms 13, workflow 29, root 25 + 1 skipped live-model test, web 1).

**Not verified / not done**
- No real LLM call has been made (model not installed at build time); `up.ps1`/`down.ps1` not re-run after Phase 3.
- No web UI, no drafting workflow (GW-01..GW-05), no entailment check, no per-user result filtering (IK-04) - still open from Phase 2.
- Keyword retrieval quality was only exercised on the synthetic corpus and one real regulation section.

### Phase 3 follow-up: first live runs + locator fix (2026-11-11)
- Live model installed (`qwen2.5:14b-instruct`, digest `7cdf5a0187d5...`). Run 1 (config 1.0.0): model claimed deviations must be reported "immediately" (SOP says within 24 hours) and cited locator `(1) Purpose` -> `invalid_citation / locator_not_found`.
- **Change CC-SYN-0002 -> AI config v1.1.0** (approved by steward-b + qa-1): the context line format and the citation instructions moved out of workflow code into the config item `prompt` (`passage_template: "[locator: {locator}] {text}"`, `instructions`); system prompt wording updated to match. Editing them without re-approval blocks AI calls (tested); a template without `{locator}`/`{text}` is refused even if approved.
- Run 2 (config 1.1.0): model cited `locator: "1"` correctly (citation structurally `ok`, quote verbatim) but made the same unsupported claim -> verifier `mismatch` (score 0.182). The guard caught it on meaning, not just format.
- **New finding (not fixed):** the number check treats document identifiers in the statement (`SOP-DEV-001`, `version 3.0`) as numbers that must appear in the passage (`number_not_in_passage:001,3.0`). That would wrongly downgrade a correct statement that names its SOP. Fix: ignore numbers that are part of the cited source's id/version.
- Now that the model is installed, `scripts	est.ps1` runs the live `llm` test too (adds ~2-4 min, CPU inference); it checks plumbing/schema only.
- Test status: gateway 84, library 22, knowledge 32, mock-eqms 13, workflow 30, root 26 (incl. live model), web 1 - all passing.

### Phase 3 follow-up: second approved model + per-call selection (code built 2026-11-11; config change PENDING)
- **Built (no model was called):** AI config now understands `models` (list of `{name, digest, generation?}`) + `default_model`. Every model must pin a digest; the gateway refuses a call if the installed digest differs (`DigestMismatch` -> 503, logged) so a silent re-pull cannot change an "approved" model. The legacy single-`model` shape (config <= 1.1.0) still loads, without digest pinning.
- `POST /assist/ask` takes an optional `model`: omitted -> `default_model`; must be in the approved list, else 400 (logged as `ai.call_refused`, Ollama never contacted). The response and the DI-03 log record which model ran, its digest, and `chosen_by` (`default` | `request`). `GET /assist/config` lists approved models, the default, whether each is installed and whether its digest matches the pin. Per-model generation overrides are supported (unused).
- **Change-control hardening found while testing:** `approve` used to re-lock whatever the file contained, even when content changed under the same `config_version`. It now refuses unless `config_version` is bumped (and requires a change reference).
- **DONE 2026-11-11: CC-SYN-0003 -> AI config v1.2.0**, approved by steward-a + qa-1 (a single approver and the unapproved edit were both refused first). Approved models, digests pinned: `qwen2.5:14b-instruct` (default, `7cdf5a0187d5...`) and `qwen2.5:7b-instruct` (`845dbda0ea48...`).
- Live test is now a comparison harness: `LIVE_MODELS="qwen2.5:14b-instruct,qwen2.5:7b-instruct"` and `LIVE_RUNS=3` (same question; prints raw output, verdicts, wall-clock, Ollama load/prompt/generation times; run order = model by model, so run 1 of each model includes model-load time). Without those variables it behaves as before (default model, 1 run).
- Tests (suites run with the live test excluded): gateway 105, library 22, knowledge 32, mock-eqms 13, workflow 35, root 25 (+1 live test deselected), web not re-run. `scripts\test.ps1` was deliberately NOT run (it would call the 14B model).

### 14B vs 7B live comparison (same question, 3 runs each, CPU only, config 1.2.0)
Question: "A deviation was reported 3 days after it was discovered. What does our SOP require for reporting time?" (correct answer in the corpus: SOP-DEV-001 v3.0 section 5.1, within 24 hours).

| Model | Run | Wall-clock | Model load | Prompt eval | Generation | Out tokens | Verifier verdict |
|---|---|---|---|---|---|---|---|
| 14B | 1 (cold) | 225.2 s | 22.4 s | 139.9 s | 59.7 s | 153 | mismatch (0.333) |
| 14B | 2 | 73.3 s | 0 | 0.3 s | 69.6 s | 171 | mismatch (0.333) |
| 14B | 3 | 72.3 s | 0 | 0.3 s | 68.5 s | 171 | mismatch (0.333) |
| 7B | 1 (cold) | 110.5 s | 12.0 s | 67.5 s | 27.5 s | 145 | **verified (0.750)** |
| 7B | 2 | 31.9 s | 0 | 0.2 s | 28.4 s | 144 | **verified (0.714)** |
| 7B | 3 | 33.2 s | 0 | 0.2 s | 29.2 s | 144 | **verified (0.714)** |

- **Speed:** 7B is ~2.3x faster warm (32 s vs 72 s) and ~2x faster cold. Cold start is dominated by prompt evaluation of the ~1,700-token context (140 s / 68 s), not by model loading; warm calls reuse the cache (prompt eval 0.2-0.3 s).
- **Quality: neither model gave the right answer** (24 hours). 14B claimed "immediately upon discovery", citing the Purpose section; the verifier flagged it as `mismatch` (correct outcome), and 14B's gaps note admitted the timeline was not in the sources in runs 2-3. 7B claimed "within two working days", citing section 5.3 - which is the QA *classification* deadline, not the reporting deadline.
- **The verifier PASSED the 7B's wrong answer ("verified", 0.71-0.75).** The citation is real, the quote is verbatim and the words overlap (the passage does contain "within two working days"), but the passage is about a different obligation. This is exactly the fluent near-miss class the stress set predicted (precision 0.25 there): lexical overlap cannot see that the passage answers a different question. It is the first real-model confirmation that a "verified" label is NOT proof of correctness and that a human check must stay in Phase 4.
- **Consequence:** on this single question the *slower* model was safer in practice because its wrong answer was caught, and the faster model's wrong answer got through. One question only; not a quality ranking. Neither model cited section 5.1; whether 5.1 reached the prompt context was not checked (worth checking: if retrieval omitted it, that is a retrieval problem, not a model problem).
- Recommended next steps (not done): an entailment check (does the passage answer *this* question / support *this* claim) before presenting "verified"; confirm section 5.1 is retrieved for this query; evaluate both models on a larger fixed question set before choosing a default.

### Lightweight retrieval fix (CC-SYN-0004, config v1.3.0) - 2026-11-11
- **Finding:** in the live comparison SOP-DEV-001 section 5.1 (the 24-hour reporting rule) was never in the model's context: it ranked 14th of 30 and `sop_top_k` was 5. So neither model could have answered correctly; the 14B/7B "quality" comparison was confounded by retrieval, not just model quality.
- **Fix 1 (code):** light word-form stemming in knowledge and library keyword search (`report/reports/reported/reporting`, `require/requires/required` now match; numbers and short words untouched). Snippets still use the unstemmed query.
- **Fix 2 (config, change-controlled):** `retrieval.sop_top_k` 5 -> 8 and method text updated. Edited -> gateway refused -> approved by steward-a + qa-1 -> v1.3.0 active. Models and pinned digests from v1.2.0 unchanged.
- **Result for the live-test question:** stemming alone moved 5.1 from rank 14 to rank 5 (right on the old cutoff); with `sop_top_k` 8 it is retrieved, and the prompt now contains `[locator: 5.1]` and "within 24 hours of detection" (prompt 4,070 chars, no truncation). Other sections retrieved for it are still partly off-target (e.g. SOP-DEV-002 4 ranks first), which is the keyword method's ceiling.
- **Not done (user instruction: stop touching Phase 3):** the 14B/7B comparison was NOT re-run with the corrected retrieval, and `scripts\test.ps1` (which includes the live model call) was not run after this change. Suites were run with the live test excluded: gateway 107, library 23, knowledge 35, mock-eqms 13, workflow 35, root 25 - all passing (web unchanged, not re-run).
- **Backlog, not blocking Phase 4:** (1) embeddings / semantic retrieval; (2) an entailment check on "verified" (the verifier cannot see that a real, verbatim passage answers a different question - confirmed with the 7B run); (3) re-run the model comparison on corrected retrieval and on a larger fixed question set before choosing a default model.

## Next
- Phase 4 (not started).
