"""
Validate evals/evals.json structure and tool name consistency.

Checks:
  - Valid JSON with required top-level keys
  - Each eval has id, prompt, expected_output, files, expectations
  - eval ids are sequential starting from 1
  - No stale tool names (e.g. vulcan_transact, vulcan_query)
  - All tool names referenced in expectations/expected_output are registered tools
"""

import json
import sys
from pathlib import Path

EVALS_PATH = Path(__file__).parent / "evals.json"

KNOWN_TOOLS = {
    "minigraf_query",
    "minigraf_transact",
    "minigraf_retract",
    "minigraf_audit",
    "minigraf_ingest_git",
    "minigraf_ingest_status",
    "minigraf_report_issue",
    "memory_prepare_turn",
    "memory_finalize_turn",
}

STALE_TOOL_NAMES = {
    "vulcan_transact",
    "vulcan_query",
    "vulcan_retract",
    "vulcan_audit",
    "vulcan_ingest_git",
    "vulcan_ingest_status",
    "vulcan_report_issue",
}

REQUIRED_EVAL_KEYS = {"id", "prompt", "expected_output", "files", "expectations"}


def validate():
    errors = []

    raw = EVALS_PATH.read_text()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        errors.append(f"evals.json is not valid JSON: {e}")
        return errors

    if "skill_name" not in data:
        errors.append("Missing top-level 'skill_name' key")
    if "evals" not in data:
        errors.append("Missing top-level 'evals' key")
        return errors

    evals = data["evals"]
    if not isinstance(evals, list) or len(evals) == 0:
        errors.append("'evals' must be a non-empty list")
        return errors

    for i, ev in enumerate(evals):
        prefix = f"Eval[{i}]"

        missing = REQUIRED_EVAL_KEYS - set(ev.keys())
        if missing:
            errors.append(f"{prefix}: missing keys {missing}")

        expected_id = i + 1
        if ev.get("id") != expected_id:
            errors.append(f"{prefix}: id={ev.get('id')!r}, expected {expected_id}")

        if not isinstance(ev.get("expectations"), list):
            errors.append(f"{prefix}: 'expectations' must be a list")
            continue

        all_text = ev.get("expected_output", "") + " ".join(ev["expectations"])

        for stale in STALE_TOOL_NAMES:
            if stale in all_text:
                errors.append(f"{prefix}: stale tool name '{stale}' found — rename to minigraf_* equivalent")

        referenced_tools = [t for t in KNOWN_TOOLS | STALE_TOOL_NAMES if t in all_text]
        unknown = [t for t in referenced_tools if t not in KNOWN_TOOLS]
        if unknown:
            errors.append(f"{prefix}: unknown tool(s) referenced: {unknown}")

    return errors


if __name__ == "__main__":
    errs = validate()
    if errs:
        print("FAIL — evals.json validation errors:")
        for e in errs:
            print(f"  {e}")
        sys.exit(1)
    else:
        evals_count = len(json.loads(EVALS_PATH.read_text())["evals"])
        print(f"OK — evals.json valid ({evals_count} evals, no stale tool names)")
