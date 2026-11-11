Read CLAUDE.md. Do Phase 0 only.

Deliver:
1. Repo layout: one folder per service (gateway, library, knowledge,
   mock-eqms, workflow, web), each a runnable FastAPI app (web is
   React+Vite). No Docker.
2. Each service has its own virtual env and requirements.txt / package.json.
3. Health endpoint on every service.
4. Enforce the closed-loop rule in code: only `gateway`'s dependencies
   include an HTTP client library (requests/httpx/aiohttp). Add a test that
   scans every other service's source and dependency file and fails if any
   networking library is imported or listed.
5. `gateway` has an allow-list config (empty for now) and a function that
   refuses any URL not on it; a test proves a non-allow-listed URL is
   rejected before any network call is attempted.
6. .env.example, PowerShell scripts in scripts/ (up.ps1 starts all services
   as background jobs, down.ps1 stops them, test.ps1 runs all tests,
   seed.ps1 is a stub for now), and docs/PROGRESS.md.

Show me the plan first and wait for my OK. When done, run the tests, show
only the summary, update docs/PROGRESS.md, and stop.

## Windows path note
- Project root is `D:\Project\QMS demo` — the folder name has a space.
  Always quote paths in PowerShell scripts and commands.

## Hard rules
- No ISO standards or other licensed/copyrighted standard content anywhere in the repo
  (code, seed data, fixtures, docs, tests). Only public-domain / openly published
  regulatory sources (e.g. eCFR, Federal Register). The gateway allow-list must never
  include a publisher of licensed standards.
- Closed loop: only `gateway` may make outbound network calls, and only to allow-listed hosts.
