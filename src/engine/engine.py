"""
engine/engine.py
-----------------
Deterministic Replay Engine — no LLM, runs every time.
"""

from __future__ import annotations
import json
import logging
import os
import time
import traceback
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Optional

from playwright.sync_api import sync_playwright

from artifact import CapabilityArtifact
from engine.executor import execute_step
from engine.extractor import check_for_business_outcome, try_recover_page, extract_output
from guardrails import Guardrails, DEFAULT_POLICY
from hitl.controller import HITLController

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    outcome_type: str
    success: bool
    outputs: dict
    business_outcome: Optional[str] = None
    error: Optional[str] = None
    failed_step_id: Optional[str] = None
    expected_state: Optional[str] = None
    observed_state: Optional[str] = None
    screenshot_path: Optional[str] = None
    steps_completed: int = 0
    duration_seconds: float = 0.0
    log_entries: list = field(default_factory=list)
    retries_used: int = 0

    def to_dict(self) -> dict:
        return asdict(self)

# def _resolve_resume_url(url_after: str, parameters: dict, artifact) -> str:
#     """
#     After a human resolves a HITL intervention, figure out what URL
#     Playwright's headless page should navigate to so extraction works.

#     Priority:
#     1. If url_after contains /member/ already — use it directly
#     2. If we have member_id in parameters — build the member detail URL
#     3. Fall back to url_after as-is
#     """
#     import urllib.parse

#     # If the user ended up on a member detail page, use it
#     if url_after and "/member/" in url_after and "/update" not in url_after:
#         return url_after

#     # Try to construct from member_id parameter
#     member_id = parameters.get("member_id", "")
#     if member_id and artifact.target_url:
#         # target_url is like http://localhost:8080/search — strip the path
#         parsed   = urllib.parse.urlparse(artifact.target_url)
#         base_url = f"{parsed.scheme}://{parsed.netloc}"
#         return f"{base_url}/member/{member_id}"

#     return url_after

class ReplayEngine:
    def __init__(
        self,
        evidence_dir: str = "evidence",
        headless: bool = True,
        max_retries_per_step: int = 2,
        hitl_enabled: bool = True,
    ):
        self.evidence_dir = evidence_dir
        self.headless = headless
        self.max_retries = max_retries_per_step
        self.hitl_enabled = hitl_enabled
        self.guardrails = Guardrails(DEFAULT_POLICY)
        self.hitl = HITLController(evidence_dir=evidence_dir)

    def run(
        self,
        artifact: CapabilityArtifact,
        parameters: dict,
        log_suffix: str = "run",
        non_interactive: bool = True,
        job_dir: str = None,
        hitl_job_id: str = None,      # ← when set, signals chat UI instead of terminal
        on_step_start=None,
        on_step_failed=None,          # optional callback(step_id, error)
    ) -> ReplayResult:
        """
        job_dir: if provided, all evidence for this run (log, screenshots,
                 interventions) goes into that directory instead of evidence_dir.
                 Used by the chat UI to keep each user message's evidence isolated.
        """
        start = time.time()
        run_log: list[dict] = []
        steps_completed = 0
        retries_used = 0

        # Where evidence lands for this specific run
        run_dir = job_dir or self.evidence_dir
        os.makedirs(run_dir, exist_ok=True)

        # HITLController scoped to this run's directory
        hitl = HITLController(evidence_dir=run_dir, job_id=hitl_job_id)

        def log(entry: dict):
            entry["ts"] = datetime.now(timezone.utc).isoformat()
            run_log.append(entry)
            logger.info(f"[replay] {json.dumps(entry)}")

        log({"event": "replay_start", "capability": artifact.name,
             "params": self.guardrails.redact_dict(parameters)})

        try:
            self.guardrails.validate_parameters(
                parameters, artifact.parameters)
        except ValueError as e:
            self._save_log(run_log, log_suffix, run_dir)
            return ReplayResult(
                outcome_type="hard_failure", success=False, outputs={},
                error=str(e), duration_seconds=time.time() - start,
                log_entries=run_log,
            )

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=self.headless,
                args=["--no-sandbox", "--start-maximized"],
                slow_mo=300 if not self.headless else 0,
            )
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            page = ctx.new_page()
            screenshot_path = None
            extracted: dict = {}

            try:
                for step in artifact.steps:
                    log({"event": "step_start", "step_id": step.step_id,
                         "action": step.action, "url": page.url})

                    if on_step_start:
                        try:
                            on_step_start(
                                step.step_id, step.action, step.description)
                        except Exception:
                            pass

                    if step.action in ("assert", "extract") or steps_completed > 2:
                        outcome = check_for_business_outcome(page)
                        if outcome:
                            log({"event": "business_outcome_detected",
                                "outcome": outcome})
                            self._save_log(run_log, log_suffix, run_dir)
                            browser.close()
                            return ReplayResult(
                                outcome_type="business_outcome", success=False,
                                outputs={}, business_outcome=outcome,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start,
                                log_entries=run_log,
                            )

                    if try_recover_page(page):
                        log({"event": "recovered_blocking_condition",
                            "step": step.step_id})
                        retries_used += 1

                    step_result = None
                    for attempt in range(self.max_retries + 1):
                        step_result = execute_step(
                            page, step, parameters, self.guardrails)
                        if step_result.get("success"):
                            break
                        if attempt < self.max_retries:
                            log({"event": "step_retry", "step_id": step.step_id,
                                 "attempt": attempt + 1, "error": step_result.get("error")})
                            page.wait_for_timeout(1000)
                            retries_used += 1
                            try_recover_page(page)

                    log({"event": "step_done", "step_id": step.step_id,
                        "result": step_result})

                    if not step_result.get("success"):
                        if on_step_failed:
                            try:
                                on_step_failed(
                                    step.step_id, step_result.get("error", ""))
                            except Exception:
                                pass
                        screenshot_path = self._screenshot(
                            page, f"failure_{step.step_id}", run_dir,
                            step_id=step.step_id,
                            reason=step_result.get("error", "Step failed"),
                        )
                        log({"event": "step_failed", "step_id": step.step_id,
                             "error": step_result.get("error"),
                             "screenshot": screenshot_path})

                        if step_result.get("blocked"):
                            self._save_log(run_log, log_suffix, run_dir)
                            browser.close()
                            return ReplayResult(
                                outcome_type="hard_failure", success=False, outputs={},
                                error=step_result["error"],
                                failed_step_id=step.step_id,
                                screenshot_path=screenshot_path,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start,
                                log_entries=run_log,
                                retries_used=retries_used,
                            )

                        if self.hitl_enabled:
                            hitl_result = hitl.escalate(
                                page=page,
                                capability_name=artifact.name,
                                goal=artifact.description,
                                current_step_id=step.step_id,
                                current_step_description=step.description,
                                reason=step_result.get("error", "Step failed"),
                                context={"parameters": parameters,
                                         "steps_completed": steps_completed},
                                non_interactive=non_interactive,
                            )

                        if hitl_result["resolved"]:
                            url_after = hitl_result.get("url_after", "")
                            log({"event": "hitl_resolved", "step_id": step.step_id,
                                 "url_after": url_after})

                            # Re-attempt the SAME step now that the human has
                            # (supposedly) fixed the page state. Do NOT construct
                            # a URL from parameters — that bypasses the human fix
                            # entirely and causes false successes.
                            retry_result = execute_step(
                                page, step, parameters, self.guardrails)

                            if retry_result.get("success"):
                                # Human genuinely fixed it — carry on
                                log({"event": "hitl_step_retry_success",
                                     "step_id": step.step_id})
                                steps_completed += 1
                                continue

                            # Step still fails after human said they fixed it
                            log({"event": "hitl_step_retry_failed",
                                 "step_id": step.step_id,
                                 "error": retry_result.get("error")})
                            screenshot_path = self._screenshot(
                                page, f"hitl_retry_failed_{step.step_id}", run_dir,
                                step_id=step.step_id,
                                reason=f"Still failed after HITL: {retry_result.get('error','')}",
                            )
                            self._save_log(run_log, log_suffix, run_dir)
                            browser.close()
                            return ReplayResult(
                                outcome_type="hard_failure",
                                success=False,
                                outputs={},
                                error=(
                                    f"Step '{step.step_id}' still failed after human "
                                    f"intervention. Check again if you are on the correct page. Error: {retry_result.get('error','')}"
                                ),
                                failed_step_id=step.step_id,
                                expected_state=step.description,
                                observed_state=f"Post-HITL retry error: {retry_result.get('error','')}",
                                screenshot_path=screenshot_path,
                                steps_completed=steps_completed,
                                duration_seconds=time.time() - start,
                                log_entries=run_log,
                                retries_used=retries_used,
                            )
                        
                    steps_completed += 1

                log({"event": "extracting_outputs"})
                for output_field in artifact.outputs:
                    try:
                        val = extract_output(
                            page, output_field, self.guardrails)
                        extracted[output_field.name] = val
                        log({"event": "output_extracted",
                             "field": output_field.name, "value": str(val)[:100]})
                    except Exception as e:
                        log({"event": "output_extraction_failed",
                             "field": output_field.name, "error": str(e)})
                        extracted[output_field.name] = None

                screenshot_path = self._screenshot(
                    page, "replay_final", run_dir)
                log({"event": "replay_complete",
                    "outputs": extracted, "steps": steps_completed})

            except Exception as e:
                screenshot_path = self._screenshot(
                    page, "replay_error", run_dir)
                log({"event": "replay_error", "error": str(e),
                     "trace": traceback.format_exc()})
                self._save_log(run_log, log_suffix, run_dir)
                browser.close()
                return ReplayResult(
                    outcome_type="hard_failure", success=False, outputs={},
                    error=str(e), screenshot_path=screenshot_path,
                    steps_completed=steps_completed,
                    duration_seconds=time.time() - start,
                    log_entries=run_log,
                )
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

        self._save_log(run_log, log_suffix, run_dir)
        return ReplayResult(
            outcome_type="success", success=True, outputs=extracted,
            steps_completed=steps_completed,
            duration_seconds=time.time() - start,
            log_entries=run_log,
            screenshot_path=screenshot_path,
            retries_used=retries_used,
        )

    def _screenshot(self, page, name: str, run_dir: str,
                    step_id: str = "", reason: str = "") -> str:
        try:
            ss_dir = os.path.join(run_dir, "screenshots")
            os.makedirs(ss_dir, exist_ok=True)
            path = os.path.join(ss_dir, f"{name}.png")
            page.screenshot(path=path)
            if reason and ("failure" in name or "hitl" in name.lower()):
                from hitl.annotator import annotate_screenshot
                annotate_screenshot(path, step_id or name,
                                    reason, page.url, path)
            return path
        except Exception:
            return ""

    def _save_log(self, run_log: list, suffix: str, run_dir: str) -> str:
        os.makedirs(run_dir, exist_ok=True)
        path = os.path.join(run_dir, f"replay_{suffix}.log")
        with open(path, "w") as f:
            for entry in run_log:
                f.write(json.dumps(entry) + "\n")
        return path
