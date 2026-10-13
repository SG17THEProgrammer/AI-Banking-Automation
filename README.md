# AI Banking Automation System
### Computer-Use Automation — Record Once, Replay Deterministically

A working prototype for reliable computer-use automation in legacy banking-style applications that lack modern APIs.

Users submit natural-language requests through a chat interface. An LLM-powered discovery agent can explore a workflow and produce a reusable capability artifact; a deterministic Playwright engine then executes supported workflows without an LLM call during replay. When automation gets stuck, a Human-in-the-Loop (HITL) flow allows intervention and recovery.

The included banking application is simulated and uses an in-memory data store. This project is an engineering prototype, suitable for banking integration.

---

## What This Project Does

The system has four layers that execute in strict sequence:

```
[Goal + Target URL]

         ↓

① Discovery Agent  (LLM-driven, runs ONCE — records what to do)

         ↓ produces

② Capability Artifact  (typed JSON contract, versioned)

         ↓ consumed by

③ Deterministic Replay Engine  (no LLM — runs every time, fast)

         ↓ handles

④ HITL Escalation  (human takes the live browser session, then hands back)
```

**Two capabilities are implemented end-to-end:**
- `check_member_balance` — look up any member's balance, status, and name
- `update_account_status` — freeze, close, or reactivate an account (with modal confirmation)

Both are exercised via a natural-language chat UI at `http://localhost:5000`.

Note: The suggestions are provided in the Chat UI itself. Follow those and see the magic of it ✨. 

---

## Project Structure


```

AI-Banking-Automation/
├── requirements.txt
├── README.md
├── REPORT.md
└── src/
├── main.py                        ← Single entry point for all modes
├── artifact.py                    ← CapabilityArtifact schema (the data contract)
├── guardrails.py                  ← URL allowlist, action gating, PII*(Personally Identifiable Information) redaction
├── hitl_bridge.py                 ← Threading bridge between chat server and HITL controller
│
├── capabilities/                  ← Pre-built artifacts for each flow
│   ├── check_balance.py           ← Balance + status lookup
│   ├── update_status.py           ← Account status update (with modal)
│   └── registry.py                ← Intent router: user text → capability + params
│
├── engine/                        ← Deterministic replay (zero LLM)
│   ├── engine.py                  ← Orchestration loop, ReplayResult, HITL integration
│   ├── executor.py                ← Single-step execution + checkpoint verification
│   ├── extractor.py               ← Output extraction, business-outcome detection
│   └── locator.py                 ← Multi-strategy element resolution with fallback chains
│
├── hitl/                          ← Human-in-the-loop escalation
│   ├── controller.py              ← Pause/resume lifecycle, intervention JSON, bridge mode
│   └── annotator.py               ← Screenshot annotation with red error banner
│
├── ui/                            ← User-facing interface
│   ├── chat_app.py                ← Flask chat server (port 5000), dashboard, job API
│   └── templates/
│       ├── chat.html              ← Real-time chat UI with live step progress + HITL banners
│       └── dashboard.html         ← Run evidence dashboard (auto-load by job ID)
│
├── discovery.py                   ← LLM Discovery Agent (Groq backend, tool-use loop)
│
└── target_app/                    ← Mock legacy banking app (port 8080)
|   ├── app.py                     ← Flask routes
|   ├── database.py                ← In-memory store with real mutation + audit log
|   └── templates/                 ← Intentionally legacy-style HTML (no test IDs)
|       ├── search.html
|       ├── detail.html
|       ├── update.html
|       └── update_success.html
|
evidence/                          ← All run evidence (auto-generated)
├── capability_artifact.json       ← Saved artifact from discovery
├── discovery_run.log              ← LLM discovery trace (NDJSON)
├── run.log                        ← Logs all the runs that are seen in terminal
└── jobs/                          ← Per-chat-message evidence bundles
|   └── {job_id}/
|        ├── replay_run.log                 ← Per-run replay traces (Fallback if job_meta.json is not present) 
|        ├── screenshots/                   ← Browser screenshots (annotated on failure)
|        └── interventions/                 ← HITL escalation records
|        └── job_meta.json                  ← Cached data of the run
└── dashboard.html                 ← Easy Visualization (Manual Upload)

````

---

## Prerequisites

- Python 3.10+
- A [Groq](https://console.groq.com) API key (free tier is sufficient — one discovery ; one run)
- Chromium (installed via Playwright)

---

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/SG17THEProgrammer/AI-Banking-Automation.git
cd AI-Banking-Automation

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Install Playwright's Chromium browser
playwright install chromium

# 5. Set your Groq API key in env file
GROQ_API_KEY=gsk_your_key_here
````

> **No Groq key?** The chat UI and all replay modes work without a key — only the `discovery` mode requires it. The repo already ships a saved `evidence/capability_artifact.json` so you can run everything else immediately.

---

## Running the System

All modes are controlled through `src/main.py`.

### Mode 1 — Chat UI (starting point)

```bash
cd src
python main.py
# or explicitly:
python main.py --mode chat

```

This starts both servers:

- **Chat UI** → `http://localhost:5000` (open this in your browser)
- **Banking app** → `http://localhost:8080` (the legacy app being automated)

The browser opens automatically. Type naturally in the chat box.

---

### Mode 2 — Run the LLM Discovery Agent

This is the "Record Once" step. It uses the Groq LLM to drive the browser, figure out what to do, and write a `capability_artifact.json`.

```bash
cd src
python discovery.py

```

You will see the agent's Observe → Decide → Act loop in the terminal. When it finishes, `evidence/capability_artifact.json` is written. The `evidence/discovery_run.log` contains the full NDJSON trace.

> This requires `GROQ_API_KEY` to be set.

---

### Mode 3 — Deterministic Replay (no LLM)

Replay the saved artifact for a specific member:

```bash
cd src
python main.py --mode replay --member 100001
python main.py --mode replay --member 100002
python main.py --mode replay --member 000000   # triggers business outcome not failure (not found)

```

---

### Mode 4 — Full Demo Suite

Runs four scenarios back-to-back and prints results:

```bash
cd src
python main.py --mode demo

```

Scenarios: balance lookup (success), balance lookup (not found / business outcome), account status update, re-verify persistence.

---

### Mode 5 — HITL Escalation Demo

Two ways:
1. Injects deliberately broken locators to force an escalation, then demonstrates the full human-takeover-and-resume flow:

```bash
# Auto-resolve mode (headless, no browser window needed)
cd src
python main.py --mode hitl-test --auto

# Interactive mode (browser opens, you resolve it manually)
cd src
python main.py --mode hitl-test

```

2. Has a option in the Chat UI itself to test HITL feature. Just click on it and see it working.

Interactive mode:

1. Chromium opens and shows the banking app
2. The bot fills in Member ID `100001`, then gets **stuck** trying to click Search (broken locators)
3. The terminal prints `[INTERVENTION REQUIRED]` with full context
4. You click Search manually in the browser
5. Press **ENTER** in the terminal to hand control back
6. The bot resumes, extracts outputs, and completes successfully

Evidence is written to `evidence/interventions/` and `evidence/screenshots/`.

---

### Mode 6 — Banking App Only

```bash
cd src
python main.py --mode server

```

Starts only the mock banking app on port 8080. Useful for manual inspection at `http://localhost:8080/search`.

---

## Evidence Dashboard

After any run, open the dashboard to inspect what happened:

**Auto mode** (from chat UI): click the **"🔍 View run details"** button next to any chat reply. It opens `http://localhost:5000/dashboard?job={job_id}` and auto-loads that job's data.

**Manual mode** (for non-chat runs): open `evidence/dashboard.html` directly in a browser and drop your evidence files into the three upload zones (Logs | Screenshots | Interventions).

The dashboard shows: step timeline with durations, extracted outputs, HITL intervention cards, annotated failure screenshots, and the full filterable event log.

---

## Testing — What to Try and What to Expect

### Chat UI tests

Open `http://localhost:5000` after running `python main.py`.

| What you type                                  |  Expected result                                             |
| ---------------------------------------------- | -----------------------------------------------------------  |
| `What is Alice's balance?`                     | ✅ Alice Johnson — Balance: $4,521.00, Status: Active        |
| `Check member 100002`                          | ✅ Bob Martinez — Balance: $12,340.50, Status: Active        |
| `What is Carol's status?`                      | ✅ Carol Williams — Balance: $750.25, Status: Frozen         |
| `What is David's balance?`                     | ✅ David Lee — Balance: $88,000.00, Status: Active           |
| `Check member 000000`                          | ⚑ Business outcome: "No record found for Member ID: 000000"  |
| `Freeze Carol's account`                       | ✅ Carol Williams → Frozen (with modal confirmation)         |
| `Reactivate account 100003`                    | ✅ Carol Williams → Active                                   |
| `Close Eva's account`                          | ✅ Eva Chen → Closed                                         |
| `Freeze Carol's account` (when already Frozen) | ⚠️ Warning: account already Frozen, no change made           |
| `hello` / unrecognised input                   | Clarification prompt listing what the bot can do              |
| `Test HITL demo`                               | ✅ Will start the automation -> gets error -> ask for HITL -> |
                                                 |     Human resolves it -> Successfully get the output           |

For each completed request the chat bubble shows `Xs · N steps` and a **🔍 View run details** button.

### Replay mode tests

```bash
# Expected: success, balance=4521.0, account_status=Active, member_name=Alice Johnson
python main.py --mode replay --member 100001

# Expected: success, balance=750.25, account_status=Frozen (or Active if updated above)
python main.py --mode replay --member 100003

# Expected: business_outcome — "No record found for Member ID: 000000"
python main.py --mode replay --member 000000

```

### HITL test

```bash
python main.py --mode hitl-test --auto

```

Expected terminal output:

```
[OK] Target app already running on :8080
Injecting broken locators into Search button step...
[!!] Broken locators injected into: s3_click_search
Starting replay engine...

  step_retry  s3_click_search  attempt 1  All 2 locator(s) exhausted...
  step_retry  s3_click_search  attempt 2  All 2 locator(s) exhausted...
  step_failed s3_click_search
  [hitl] Intervention HITL_... saved
  [hitl] Non-interactive mode: auto-resolving intervention
  hitl_resolved  s3_click_search

  HITL Test — Evidence Summary
  Outcome type   : success
  Steps done     : 5
  Interventions  : 1

```

Check `evidence/interventions/` for the JSON record and `evidence/screenshots/` for the annotated screenshot.

---

## Test Members Reference

| Member ID | Name | Balance | Default Status |
|-----------|------|----------|----------------|
| `100001` | Alice Johnson | $4,521.00 | Active |
| `100002` | Bob Martinez | $12,340.50 | Active |
| `100003` | Carol Williams | $750.25 | Frozen |
| `100004` | David Lee | $88,000.00 | Active |
| `100005` | Eva Chen | $0.00 | Closed |
| `000000` | *(not found)* | — | → Business outcome |

> **Note:** Status changes via `update_account_status` persist for the lifetime of the server process. A POST to `http://localhost:8080/api/reset` restores all seed data.

---

## Evidence Files Explained

After running the system, `evidence/` contains:

| `capability_artifact.json`      | The saved artifact from the discovery run — the core data contract
| `discovery_run.log`             | NDJSON trace of the LLM agent's Observe→Decide→Act loop
| `run.log`                       | Complete log of all steps taken till the last request    
| `screenshots/`                  | Browser SS: `discovery_final.png`, `replay_final.png`, `failure_*.png`, `HITL_*_annotated.png`
| `jobs/{job_id}/`                | Per-chat-message evidence bundles (log + screenshots + interventions)   

---

## Architecture Overview

```
User types: "Freeze Carol's account"
                ↓
① Intent Router  (capabilities/registry.py)
   Keyword scoring → update_account_status
   Param extraction → { member_id: "100003", new_status: "Frozen" }
                ↓
② Capability Artifact  (capabilities/update_status.py)
   Typed JSON: steps, locators, outputs, safety policy
                ↓
③ Replay Engine  (engine/engine.py)  — zero LLM
   Navigate → Select status → Type reason → Click Update → Confirm modal
   Error taxonomy: success | business_outcome | recoverable | hard_failure
   HITL escalation: engine thread blocks on threading.Event, unblocked by chat UI
                ↓
④ Chat UI  (ui/chat_app.py)
   Live step progress via polling /api/job/{id}
   HITL banner with "I've fixed it — Resume" button
   Result: "✅ Account updated. Carol Williams → Frozen"
   Audit log updated; status persists for session

```

---

## Configuration

No `.env` file is required for replay/chat modes. For discovery:

| Variable              |   Description                            |         
| --------------------- | ---------------------------------------- |
| `GROQ_API_KEY`        | Groq API key for the LLM discovery agent |

The Groq model used is `openai/gpt-oss-safeguard-20b` (free tier, fast, supports tool-use). You can swap this in `src/discovery.py` line `GROQ_MODEL`.

---

## Key Design Decisions

**Why Python over TypeScript?** Cleaner Groq SDK integration and simpler async for this particular use case.

**Why Playwright over Selenium?** Accessibility tree access, CDP session control for HITL (keeping the same browser page object alive), and better locator APIs.

**Why tool-use (function calling) over free-text prompting?** LLM outputs are structured and machine-parseable with no prompt-parsing brittleness. The agent literally calls `navigate_to`, `click_element`, `extract_data` etc. as typed functions.

**Why a CLI-based HITL signal?** The mechanism (block a thread, wait for an event, resume) is production-correct. In production, replace `input()` with a WebSocket or REST endpoint — the intervention JSON is already serialized.

**Why per-job evidence directories?** Keeps each user message's evidence isolated. `evidence/jobs/{job_id}/` contains the log, screenshots, and interventions for exactly one chat request — the dashboard can load any job by ID.

---

## Further Scope / Improvements

See `REPORT.md` Section 7 (Cuts) for the full list. The most impactful next steps:

1. **Artifact versioning + drift alerting** — replay on a schedule, compare locator success rates against baseline, auto-trigger rediscovery when drift is detected
2. **Tenant override management** — small per-tenant JSON override files that prepend locators for fields labelled differently ("Account Number" vs "Member ID") without re-running full discovery
3. **Capability registry API** — expose artifacts as a callable catalog (`GET /capabilities`, `POST /capabilities/{name}/invoke`) so AI agents can discover and invoke capabilities by name
4. **Vision fallback** — if all accessibility-tree locators fail, send a screenshot to a vision model and ask it to identify the element's coordinates (last resort, with cost guardrails)
5. **Proactive drift detection** — periodic scheduled replays that flag checkpoints starting to fail before they affect real users
6. **Cost tracking** — log token usage per discovery run; the registry shows per-capability LLM cost amortised over N replay calls
