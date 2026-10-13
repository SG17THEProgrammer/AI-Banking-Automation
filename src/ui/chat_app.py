"""
ui/chat_app.py
--------------
User-facing chat server (port 5000).
"""

from __future__ import annotations
from engine.engine import ReplayEngine
from capabilities.registry import match, list_capabilities
import base64
import json
import logging
import os
import sys
import threading
import time
import uuid
import urllib.request
import hitl_bridge

from flask import Flask, render_template, request, jsonify

_UI_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_UI_DIR)
sys.path.insert(0, _SRC)


logger = logging.getLogger(__name__)
app = Flask(__name__, template_folder=os.path.join(_UI_DIR, "templates"))

EVIDENCE_DIR = os.path.join(_SRC, "..", "evidence")
JOBS_DIR = os.path.join(EVIDENCE_DIR, "jobs")
BANKING_URL = "http://localhost:8080"

_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _save_job_meta(job_id: str, job: dict):
    """Persist completed job metadata so it survives server restarts."""
    try:
        job_dir = os.path.join(JOBS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        meta = {k: v for k, v in job.items() if k != "log_entries"}
        with open(os.path.join(job_dir, "job_meta.json"), "w") as f:
            json.dump(meta, f, indent=2)
    except Exception as e:
        logger.warning(f"[chat] Could not save job meta for {job_id}: {e}")


def _load_job_from_disk(job_id: str):
    """
    Load job metadata from disk.
    1. Try job_meta.json (written by new code going forward).
    2. Fall back to reconstructing from replay_run.log (old job folders).
    3. Save job_meta.json so the next request is instant.
    """
    job_dir = os.path.join(JOBS_DIR, job_id)
    if not os.path.isdir(job_dir):
        return None

    # Fast path — already have persisted meta
    meta_path = os.path.join(job_dir, "job_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path) as f:
                return json.load(f)
        except Exception:
            pass  # corrupted — fall through and rebuild

    # Slow path — reconstruct from replay_run.log
    log_path = os.path.join(job_dir, "replay_run.log")
    if not os.path.exists(log_path):
        return None

    try:
        entries = []
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass

        if not entries:
            return None

        # Pull key events
        start_ev   = next((e for e in entries if e.get("event") == "replay_start"), {})
        done_ev    = next((e for e in entries if e.get("event") == "replay_complete"), None)
        bo_ev      = next((e for e in entries if e.get("event") == "business_outcome_detected"), None)
        fail_ev    = next((e for e in entries if e.get("event") == "replay_error"), None)

        capability = start_ev.get("capability", "check_member_balance")
        params     = start_ev.get("params", {})

        # Derive timing
        def _ts(e):
            t = e.get("ts") or e.get("timestamp", "")
            try:
                from datetime import datetime, timezone
                return datetime.fromisoformat(t.replace("Z", "+00:00"))
            except Exception:
                return None

        t_start = _ts(entries[0])
        t_end   = _ts(entries[-1])
        duration = round((t_end - t_start).total_seconds(), 2) if t_start and t_end else 0

        steps_completed = sum(1 for e in entries if e.get("event") == "step_done"
                              and e.get("result", {}).get("success"))

        # Determine outcome
        if done_ev:
            outcome  = "success"
            outputs  = done_ev.get("outputs", {})
            success  = True
            error    = None
            bo_msg   = None
        elif bo_ev:
            outcome  = "business_outcome"
            outputs  = {}
            success  = False
            error    = None
            bo_msg   = bo_ev.get("outcome", "")
        else:
            outcome  = "hard_failure"
            outputs  = {}
            success  = False
            error    = (fail_ev or {}).get("error", "Unknown error")
            bo_msg   = None

        # Build a reply string
        member_id = params.get("member_id", "")
        if outcome == "success":
            name    = outputs.get("member_name") or outputs.get("updated_status") or f"member {member_id}"
            balance = outputs.get("balance")
            status  = outputs.get("account_status") or outputs.get("updated_status", "")
            if balance is not None:
                bal_str = f"${float(balance):,.2f}" if balance else "—"
                reply = f"✅ **{name}**\n• Balance: **{bal_str}**\n• Status: **{status}**"
            else:
                reply = f"✅ **{name}** — {status}"
        elif outcome == "business_outcome":
            reply = f"⚑ {bo_msg}"
        else:
            reply = f"❌ Hard failure: {error or 'unknown'}"

        meta = {
            "status":            "done",
            "outcome":           outcome,
            "success":           success,
            "outputs":           outputs,
            "business_outcome":  bo_msg,
            "error":             error,
            "duration":          duration,
            "steps":             steps_completed,
            "capability":        capability,
            "reply":             reply,
            "job_dir":           job_dir,
            "_reconstructed":    True,   # flag so you know it was rebuilt
        }

        # Cache it so next load is instant
        try:
            with open(meta_path, "w") as f:
                json.dump(meta, f, indent=2)
        except Exception:
            pass

        return meta

    except Exception as e:
        logger.warning(f"[chat] Could not reconstruct meta for {job_id}: {e}")
        return None

def _run_hitl_demo(job_id: str):
    """Run check_balance with deliberately broken locators to trigger HITL from chat."""
    from artifact import Step, Locator
    import capabilities.check_balance as cap_check_balance

    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        hitl_bridge.register_job(job_id)

        def _on_step(step_id, action, description):
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["live_step"] = {"step_id": step_id, "action": action, "description": description}

        def _on_step_failed(step_id, error):
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["live_failed_step"] = step_id
                    _jobs[job_id]["live_step"] = {"step_id": step_id, "action": "failed", "description": error[:120]}

        artifact = cap_check_balance.build("http://localhost:8080")
        # Break the click-search step locators to force HITL
        for i, step in enumerate(artifact.steps):
            if step.action == "click" and "search" in step.step_id.lower():
                artifact.steps[i] = Step(
                    step_id=step.step_id,
                    action="click",
                    locators=[
                        Locator("css",        "button.__hitl_demo_broken__",  "Broken — triggers HITL"),
                        Locator("aria-label", "__hitl_demo_broken_fallback__","Broken fallback"),
                    ],
                    description=step.description + " [HITL DEMO — locators intentionally broken]",
                    checkpoint=step.checkpoint,
                    wait_after_ms=300,
                    is_reversible=step.is_reversible,
                )
                break

        engine = ReplayEngine(
            evidence_dir=EVIDENCE_DIR,
            headless=True,
            max_retries_per_step=1,
            hitl_enabled=True,
        )
        result = engine.run(
            artifact=artifact,
            parameters={"member_id": "100001"},
            log_suffix="run",
            non_interactive=False,
            job_dir=job_dir,
            hitl_job_id=job_id,
            on_step_start=_on_step,
            on_step_failed=_on_step_failed,
        )
        with _lock:
            _jobs[job_id] = {
                "status":      "done",
                "outcome":     result.outcome_type,
                "success":     result.success,
                "outputs":     result.outputs,
                "error":       result.error,
                "duration":    round(result.duration_seconds, 2),
                "steps":       result.steps_completed,
                "reply":       _format_reply("check_member_balance", result, {"member_id": "100001"}, "Alice Johnson (HITL DEMO)"),
                "log_entries": result.log_entries,
                "job_dir":     job_dir,
            }
        _save_job_meta(job_id, _jobs[job_id])
        hitl_bridge.cleanup(job_id)

    except Exception as e:
        logger.exception(f"[chat] HITL demo job {job_id} failed")
        with _lock:
            _jobs[job_id] = {"status": "done", "outcome": "hard_failure", "success": False,
                             "error": str(e), "reply": f"❌ HITL demo error: {e}", "log_entries": [], "job_dir": job_dir}
        _save_job_meta(job_id, _jobs[job_id])


# ── Suppress /health and /api/health-proxy from Werkzeug access log ────────

class _HealthFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "/health" in msg:
            return False
        if "/api/health-proxy" in msg:
            return False
        if "/api/job/" in msg and ("GET" in msg) and ("200" in msg):
            return False
        return True

# filter out health check logs from your Flask/Werkzeug server
logging.getLogger("werkzeug").addFilter(_HealthFilter())

# Werkzeug is the foundational German Swiss-army knife utility library that Flask uses to handle web requests.
# The name means "tool" or "toolkit"

# We can change its display name to whatever we want
# werkzeug_logger.name = "any_suitable_name"

# ── Pages ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("chat.html", capabilities=list_capabilities())


@app.route("/dashboard")
def dashboard():
    return render_template("dashboard.html")


# ── Chat API ──────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data = request.get_json(force=True)
    message = (data.get("message") or "").strip()
        # HITL demo: user types "test hitl" or "simulate hitl" to trigger a real escalation
    hitl_trigger_phrases = ["test hitl", "simulate hitl", "demo hitl", "trigger hitl", "hitl demo"]
    if any(p in message.lower() for p in hitl_trigger_phrases):
        job_id = str(uuid.uuid4())
        with _lock:
            _jobs[job_id] = {"status": "running", "capability": "check_member_balance", "started": time.time()}
        threading.Thread(
            target=_run_hitl_demo,
            args=(job_id,),
            daemon=True,
        ).start()
        return jsonify({
            "job_id":        job_id,
            "type":          "accepted",
            "capability":    "check_member_balance",
            "parameters":    {"member_id": "100001"},
            "subject_label": "Alice Johnson (ID 100001) [HITL DEMO]",
        })
    if not message:
        return jsonify({"error": "empty message"}), 400

    job_id = str(uuid.uuid4())
    cap = match(message)

    if cap is None:
        return jsonify({
            "job_id": job_id,
            "type":   "no_match",
            "reply": (
                "I didn't understand that. I can help you:\n"
                "• **Check a balance** — e.g. 'What is Alice's balance?' or 'check member 100002'\n"
                "• **Update account status** — e.g. 'freeze Carol's account' or 'close account 100005'\n\n"
                "Just tell me which member and what you'd like to do."
            ),
        })

    if cap.missing:
        prompts = {
            "member_id":  "Which member? Use their name (Alice, Bob…) or their 6-digit ID.",
            "new_status": "What should the new status be? (Active / Frozen / Closed)",
        }
        asks = [prompts.get(k, f"Please provide: {k}") for k in cap.missing]
        return jsonify({
            "job_id":  job_id,
            "type":    "needs_info",
            "reply":   "\n".join(asks),
            "missing": cap.missing,
        })

    subject_label = _subject_label(cap.parameters)

    if cap.name == "update_account_status":
        warning = _check_already_same_status(cap.parameters)
        if warning:
            return jsonify({
                "job_id":  job_id,
                "type":    "warning",
                "reply":   f"No update needed for {subject_label}.",
                "warning": warning,
            })

    with _lock:
        _jobs[job_id] = {"status": "running",
                         "capability": cap.name, "started": time.time()}

    threading.Thread(
        target=_run,
        args=(job_id, cap, subject_label),
        daemon=True,
    ).start()

    return jsonify({
        "job_id":        job_id,
        "type":          "accepted",
        "capability":    cap.name,
        "parameters":    cap.parameters,
        "subject_label": subject_label,
    })


@app.route("/api/job/<job_id>")
def job_status(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        job = _load_job_from_disk(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    
    # Inject live HITL context if waiting
    if hitl_bridge.is_waiting(job_id):
        ctx = hitl_bridge.get_context(job_id)
        job["status"] = "hitl_waiting"
        job["hitl_step"] = ctx.get("step_id")
        job["hitl_reason"] = ctx.get("reason")
        job["hitl_url"] = ctx.get("url")
        # Keep live_step and live_failed_step so progress bubble shows ❌
    job["job_id"] = job_id
    return jsonify(job)


@app.route("/api/job/<job_id>/details")
def job_details(job_id: str):
    """
    Return everything about a completed job:
    - the log entries
    - screenshots as base64
    - intervention JSONs
    Used by the dashboard when opened with ?job={job_id}.
    """
    with _lock:
        job = dict(_jobs.get(job_id, {}))
    if not job:
        job = _load_job_from_disk(job_id) or {}
    if not job:
        return jsonify({"error": "unknown job"}), 404

    job_dir = os.path.join(JOBS_DIR, job_id)

    # ── Log entries ───────────────────────────────────────────────────────
    log_entries = job.get("log_entries", [])
    if not log_entries:
        # Try all known log filename patterns
        candidate_logs = [
            os.path.join(job_dir, "replay_run.log"),
            os.path.join(job_dir, "replay.log"),
            os.path.join(job_dir, "replay_success.log"),
            os.path.join(job_dir, "replay_hitl_test.log"),
        ]
        # Also pick up any .log file in the job dir as a last resort
        try:
            for fname in os.listdir(job_dir):
                if fname.endswith(".log"):
                    p = os.path.join(job_dir, fname)
                    if p not in candidate_logs:
                        candidate_logs.append(p)
        except Exception:
            pass

        for log_path in candidate_logs:
            if os.path.exists(log_path):
                log_entries = []
                with open(log_path) as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                log_entries.append(json.loads(line))
                            except Exception:
                                pass
                if log_entries:
                    break  # found a non-empty log, stop looking

    # ── Screenshots as base64 ─────────────────────────────────────────────
    screenshots = []
    ss_dir = os.path.join(job_dir, "screenshots")
    if os.path.isdir(ss_dir):
        for fname in sorted(os.listdir(ss_dir)):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                fpath = os.path.join(ss_dir, fname)
                try:
                    with open(fpath, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    screenshots.append({
                        "name": fname,
                        "size": os.path.getsize(fpath),
                        "data": f"data:image/png;base64,{b64}",
                    })
                except Exception:
                    pass

    # ── Intervention JSONs ────────────────────────────────────────────────
    interventions = []
    iv_dir = os.path.join(job_dir, "interventions")
    if os.path.isdir(iv_dir):
        for fname in sorted(os.listdir(iv_dir)):
            if fname.endswith(".json"):
                try:
                    with open(os.path.join(iv_dir, fname)) as f:
                        interventions.append(json.load(f))
                except Exception:
                    pass

    return jsonify({
        "job_id":       job_id,
        "status":       job.get("status"),
        "outcome":      job.get("outcome"),
        "capability":   job.get("capability"),
        "duration":     job.get("duration"),
        "steps":        job.get("steps"),
        "outputs":      job.get("outputs", {}),
        "reply":        job.get("reply", ""),
        "log_entries":  log_entries,
        "screenshots":  screenshots,
        "interventions": interventions,
    })


# ── Proxy endpoints ────────────────────────────────────────────────────────

@app.route("/api/audit-log")
def audit_log():
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/audit-log", timeout=3) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/job/<job_id>/resolve", methods=["POST"])
def job_resolve(job_id: str):
    """Human has resolved the intervention — unblock the engine thread."""
    if not hitl_bridge.is_waiting(job_id):
        return jsonify({"ok": True, "already_resolved": True})
    notes = (request.get_json(force=True) or {}).get("notes", "")
    hitl_bridge.resolve(job_id, notes)
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["status"] = "running"
    return jsonify({"ok": True})


@app.route("/api/job/<job_id>/hitl")
def job_hitl_status(job_id: str):
    """Returns current HITL context if the job is waiting for intervention."""
    if not hitl_bridge.is_waiting(job_id):
        return jsonify({"waiting": False})
    ctx = hitl_bridge.get_context(job_id)
    return jsonify({"waiting": True, **ctx})


@app.route("/api/health-proxy")
def health_proxy():
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/health", timeout=2) as r:
            return app.response_class(r.read(), mimetype="application/json")
    except Exception:
        return jsonify({"status": "offline"}), 502


# ── Background worker ─────────────────────────────────────────────────────

def _run(job_id: str, cap, subject_label: str):
    # Per-job evidence directory
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        hitl_bridge.register_job(job_id)

        def _on_step_failed(step_id: str, error: str):
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["live_failed_step"] = step_id
                    _jobs[job_id]["live_step"] = {
                        "step_id":     step_id,
                        "action":      "failed",
                        "description": error[:120],
                    }

        # Live progress: write current step to job dict during execution
        # The engine calls this via a callback
        def _on_step(step_id: str, action: str, description: str):
            with _lock:
                if job_id in _jobs:
                    _jobs[job_id]["live_step"] = {
                        "step_id":     step_id,
                        "action":      action,
                        "description": description,
                    }

        engine = ReplayEngine(
            evidence_dir=EVIDENCE_DIR,
            headless=True,
            max_retries_per_step=2,
            hitl_enabled=True,
        )

        artifact = cap.artifact
        params = cap.parameters

        if artifact.name == "update_account_status":
            _patch_update_url(artifact, params)

        result = engine.run(
            artifact=artifact,
            parameters=params,
            log_suffix="run",
            non_interactive=False,
            job_dir=job_dir,
            hitl_job_id=job_id,        # engine uses this to signal UI
            on_step_start=_on_step,
            on_step_failed=_on_step_failed,
        )

        with _lock:
            _jobs[job_id] = {
                "status":        "done",
                "outcome":       result.outcome_type,
                "success":       result.success,
                "outputs":       result.outputs,
                "business_outcome": result.business_outcome,
                "error":         result.error,
                "duration":      round(result.duration_seconds, 2),
                "steps":         result.steps_completed,
                "reply":         _format_reply(cap.name, result, params, subject_label),
                "log_entries":   result.log_entries,
                "job_dir":       job_dir,
            }
            # Clean up the resume event
            hitl_bridge.cleanup(job_id)

        _save_job_meta(job_id, _jobs[job_id])

    except Exception as e:
        logger.exception(f"[chat] Job {job_id} failed")
        with _lock:
            _jobs[job_id] = {
                "status":  "done",
                "outcome": "hard_failure",
                "success": False,
                "error":   str(e),
                "reply":   f"❌ Automation error: {e}",
                "log_entries": [],
                "job_dir": job_dir,
            }

        _save_job_meta(job_id, _jobs[job_id])


# ── Helpers ───────────────────────────────────────────────────────────────

def _subject_label(params: dict) -> str:
    mid = params.get("member_id", "")
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/member/{mid}", timeout=2) as r:
            data = json.loads(r.read())
            name = data.get("name", "")
            if name:
                return f"{name} (ID {mid})"
    except Exception:
        pass
    return f"member {mid}"


def _check_already_same_status(params: dict) -> str | None:
    mid = params.get("member_id", "")
    new_status = params.get("new_status", "")
    if not mid or not new_status:
        return None
    try:
        with urllib.request.urlopen(f"{BANKING_URL}/api/member/{mid}", timeout=2) as r:
            data = json.loads(r.read())
        current = data.get("account_status", "")
        name = data.get("name", f"Member {mid}")
        if current.lower() == new_status.lower():
            return f"{name}'s account is already {current}. No changes were made."
    except Exception:
        pass
    return None


def _patch_update_url(artifact, params: dict):
    mid = params.get("member_id", "")
    for step in artifact.steps:
        if step.action == "navigate" and step.input_value and "{member_id}" in step.input_value:
            step.input_value = step.input_value.replace("{member_id}", mid)
            step.input_var = None
            break


def _format_reply(capability: str, result, params: dict, subject_label: str) -> str:
    if result.outcome_type == "business_outcome":
        return f"⚑ {result.business_outcome}"
    if result.outcome_type == "hard_failure":
        step = result.failed_step_id or "unknown"
        return f"❌ Automation failed at step `{step}`.\n{result.error or ''}"
    if capability == "check_member_balance":
        name = result.outputs.get("member_name") or subject_label
        balance = result.outputs.get("balance")
        status = result.outputs.get("account_status", "")
        bal_str = f"${balance:,.2f}" if isinstance(
            balance, (int, float)) else str(balance)
        return f"✅ **{name}**\n• Balance: **{bal_str}**\n• Status: **{status}**"
    if capability == "update_account_status":
        name = result.outputs.get("member_name") or subject_label
        new_status = result.outputs.get(
            "updated_status") or params.get("new_status", "")
        return f"✅ Account updated for **{name}**.\n• New status: **{new_status}**"
    return f"✅ Done. {result.outputs}"


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    os.makedirs(JOBS_DIR, exist_ok=True)
    print("\n  Chat UI → http://localhost:5000")
    print("  Banking app must be running on http://localhost:8080\n")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
