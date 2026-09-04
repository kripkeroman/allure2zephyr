#!/usr/bin/env python3
"""
allure2zephyr_dc.py — upload allure-results to Zephyr Scale Server/DC.

Flow:
  1. Read target/allure-results, take case keys from @TmsLink (Allure tms links).
  2. bulk/get by keys -> resolve KEY-T... -> numeric testCaseId (key != id).
  3. POST /testrun -> create a Test Cycle in a folder.
  4. PUT  /testrunitem/bulk/save -> add cases to the cycle (creates testRunItems).
  5. GET  /testrun/{key} -> collect created testResult ids per case.
  6. PUT  /testresult -> set statuses (Pass/Fail/Blocked/Not Executed).

Usage:
    export JIRA_PAT=<Personal Access Token>

    python allure2zephyr_dc.py \\
        --base-url https://jira.example.com \\
        --allure-dir target/allure-results \\
        --cycle-name "E2E #42 (main)"

    python allure2zephyr_dc.py --base-url https://jira.example.com --cycle-key SM-R123

    python allure2zephyr_dc.py --base-url https://jira.example.com --dry-run
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API = "/rest/tests/1.0"
TMS_LINK_TYPES = {"tms", "test_case"}

# Result status ids from GET /project/{projectId}/testresultstatus — override with flags.
DEFAULT_STATUS_MAP = {
    "passed": 22,   # Pass
    "failed": 23,   # Fail
    "broken": 24,   # Blocked (exception, not assertion)
    "skipped": 20,  # Not Executed
}


# ------------------------------------------------------------------ allure

def load_latest_results(allure_dir: Path) -> list[dict]:
    """Read *-result.json, collapsing retries: keep the latest attempt per historyId."""
    latest: dict[str, dict] = {}
    for path in glob.glob(str(allure_dir / "*-result.json")):
        try:
            with open(path, encoding="utf-8") as fh:
                result = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  ! skip {path}: {exc}", file=sys.stderr)
            continue
        key = result.get("historyId") or result.get("fullName") or result.get("uuid")
        current = latest.get(key)
        if current is None or result.get("stop", 0) >= current.get("stop", 0):
            latest[key] = result
    return list(latest.values())


def extract_case_keys(result: dict) -> list[str]:
    """Case keys from tms links. List because @TmsLinks can point at several cases."""
    keys = []
    for link in result.get("links", []):
        if link.get("type") in TMS_LINK_TYPES:
            value = link.get("name") or ""
            m = re.search(r'([A-Z][A-Z0-9]+-T\d+)', value)
            if m:
                keys.append(m.group(1))
    return keys


def worst_status(statuses: list[str]) -> str:
    """If one case has several runs, keep the worst status (Fail > Blocked > ...)."""
    order = ["failed", "broken", "passed", "skipped"]
    for s in order:
        if s in statuses:
            return s
    return "skipped"


# ------------------------------------------------------------------ zephyr api (urllib)

class ZephyrDC:
    def __init__(self, token: str, base_url: str, project_id: int,
                 folder_id: int | None, jira_version_id: str | None,
                 cycle_status_id: int | None, owner_key: str | None):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.project_id = project_id
        self.folder_id = folder_id
        self.jira_version_id = jira_version_id
        self.cycle_status_id = cycle_status_id
        self.owner_key = owner_key

    def _request(self, method: str, path: str, payload=None) -> tuple[int, object]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            f"{self.base_url}{path}", data=data, method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "jira-project-id": str(self.project_id),
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw.strip() else None)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            return exc.code, {"_error": body}
        except urllib.error.URLError as exc:
            sys.exit(f"Network unavailable: {exc.reason}")

    def resolve_keys(self, keys: list[str]) -> dict[str, int]:
        """bulk/get: ['PROJ-T761',...] -> {'PROJ-T761': 761, ...}. Key != id; resolve is required."""
        status, body = self._request(
            "POST", f"{API}/testcase/bulk/get?fields=id,key", payload=keys)
        if status != 200 or not isinstance(body, list):
            err = body.get("_error") if isinstance(body, dict) else body
            sys.exit(f"bulk/get failed: HTTP {status}\n{err}")
        return {c["key"]: c["id"] for c in body}

    def create_cycle(self, name: str, description: str | None) -> tuple[int, str]:
        payload = {
            "projectId": self.project_id,
            "name": name,
            "folderId": self.folder_id,
            "statusId": self.cycle_status_id,
            "projectVersionId": self.jira_version_id,
            "owner": self.owner_key,
            "plannedStartDate": _now_iso(),
            "plannedEndDate": _now_iso(),
        }
        if description:
            payload["description"] = description
        status, body = self._request("POST", f"{API}/testrun", payload)
        if status not in (200, 201) or not body or "id" not in body:
            err = body.get("_error") if isinstance(body, dict) else body
            sys.exit(f"Failed to create cycle: HTTP {status}\n{err}")
        return body["id"], body["key"]

    def get_cycle(self, key: str) -> tuple[int, str, int]:
        """GET existing cycle by key -> (id, key, number of cases already in the cycle)."""
        status, body = self._request(
            "GET", f"{API}/testrun/{key}?fields=id,key,testRunItems")
        if status != 200 or not body or "id" not in body:
            err = body.get("_error") if isinstance(body, dict) else body
            sys.exit(f"Cycle {key} not found: HTTP {status}\n{err}")
        item_count = len(body.get("testRunItems") or [])
        return body["id"], body["key"], item_count

    def add_items(self, run_id: int, case_ids: list[int]) -> None:
        """PUT /testrunitem/bulk/save — add cases to the cycle (payload shape from the UI).
        index is the position INSIDE the added batch (0..N-1), not absolute in the cycle:
        the server appends them; autoReorder assigns final indexes.
        An absolute index here gets HTTP 400 'index value out of range'."""
        added = [
            {"index": i,
             "lastTestResult": {
                 "testCaseId": cid,
                 "jiraVersionId": self.jira_version_id,
                 "assignedTo": self.owner_key,
             }}
            for i, cid in enumerate(case_ids)
        ]
        payload = {
            "testRunId": run_id,
            "addedTestRunItems": added,
            "updatedTestRunItems": [],
            "updatedTestRunItemsIndexes": [],
            "deletedTestRunItems": [],
            "autoReorder": True,
        }
        status, body = self._request("PUT", f"{API}/testrunitem/bulk/save", payload)
        if status not in (200, 204):
            err = body.get("_error") if isinstance(body, dict) else body
            sys.exit(f"Failed to add cases to cycle: HTTP {status}\n{err}")
        returned = body.get("testRunItems", body) if isinstance(body, dict) else body
        n = len(returned) if isinstance(returned, list) else "?"
        print(f"  API returned testRunItems: {n} (expected {len(case_ids)})")
        if n == 0:
            print(f"  ! server accepted the request but added no cases. Response:\n{json.dumps(body, ensure_ascii=False)[:2000]}")

    def fetch_result_ids(self, run_key: str) -> dict[int, int]:
        """GET the full cycle -> {testCaseId: testResultId}. testResultId is used in PUT status."""
        status, body = self._request(
            "GET", f"{API}/testrun/{run_key}?fields=id,key,testRunItems")
        if status != 200 or not body:
            sys.exit(f"Failed to read cycle {run_key}: HTTP {status}")

        mapping: dict[int, int] = {}
        for item in body.get("testRunItems", []):
            results = item.get("testResults") or []
            if not results:
                continue
            last = results[-1]
            case_id = last.get("testCaseId") or (last.get("testCase") or {}).get("id")
            result_id = last.get("id")
            if case_id and result_id:
                mapping[case_id] = result_id
        return mapping

    def set_status(self, result_id: int, status_id: int) -> bool:
        payload = [{
            "id": result_id,
            "testResultStatusId": status_id,
            "userKey": self.owner_key,
            "executionDate": _now_iso(),
            "actualStartDate": _now_iso(),
        }]
        status, _ = self._request("PUT", f"{API}/testresult", payload)
        return status in (200, 204)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ------------------------------------------------------------------ main

def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", required=True,
                   help="Jira base URL, e.g. https://jira.example.com")
    p.add_argument("--allure-dir", default=Path("target/allure-results"), type=Path)
    p.add_argument("--cycle-name",
                   default=f"Automated run {datetime.now():%Y-%m-%d %H:%M}",
                   help="name of a new cycle (used only if --cycle-key is not set)")
    p.add_argument("--cycle-key",
                   help="existing cycle key (PROJ-R... from URL /testCycle/{key}): "
                        "upload into it instead of creating a new cycle")
    p.add_argument("--description", help="cycle description (e.g. Allure report URL in CI)")
    p.add_argument("--project-id", type=int,
                   help="Jira/Zephyr project id (required unless --dry-run)")
    p.add_argument("--folder-id", type=int,
                   help="cycle folder id (required when creating a new cycle)")
    p.add_argument("--jira-version-id",
                   help="project version id (required unless --dry-run)")
    p.add_argument("--cycle-status-id", type=int,
                   help="status id of a newly created cycle")
    p.add_argument("--owner-key",
                   help="cycle owner / assignedTo, e.g. JIRAUSER123 (required unless --dry-run)")
    p.add_argument("--status-passed", type=int, default=DEFAULT_STATUS_MAP["passed"])
    p.add_argument("--status-failed", type=int, default=DEFAULT_STATUS_MAP["failed"])
    p.add_argument("--status-broken", type=int, default=DEFAULT_STATUS_MAP["broken"])
    p.add_argument("--status-skipped", type=int, default=DEFAULT_STATUS_MAP["skipped"])
    p.add_argument("--dry-run", action="store_true")
    return p.parse_args()


def status_map_from_args(args) -> dict[str, int]:
    return {
        "passed": args.status_passed,
        "failed": args.status_failed,
        "broken": args.status_broken,
        "skipped": args.status_skipped,
    }


def require_live_args(args) -> None:
    missing = []
    if args.project_id is None:
        missing.append("--project-id")
    if args.jira_version_id is None:
        missing.append("--jira-version-id")
    if args.owner_key is None:
        missing.append("--owner-key")
    if not args.cycle_key:
        if args.folder_id is None:
            missing.append("--folder-id")
        if args.cycle_status_id is None:
            missing.append("--cycle-status-id")
    if missing:
        sys.exit("Missing required flags for a live run: " + ", ".join(missing))


def main() -> None:
    args = parse_args()
    status_map = status_map_from_args(args)

    if not args.allure_dir.is_dir():
        sys.exit(f"Directory {args.allure_dir} not found — did the tests actually run?")

    results = load_latest_results(args.allure_dir)

    case_statuses: dict[str, list[str]] = {}
    skipped_tests = 0
    for result in results:
        keys = extract_case_keys(result)
        if not keys:
            skipped_tests += 1
            continue
        st = result.get("status", "skipped")
        for key in keys:
            case_statuses.setdefault(key, []).append(st)

    resolved_status = {k: worst_status(v) for k, v in case_statuses.items()}

    print(f"Results read:           {len(results)}")
    print(f"Cases with @TmsLink:    {len(resolved_status)}")
    print(f"Tests without a link:   {skipped_tests}")

    if not resolved_status:
        sys.exit("No tests with a tms link — check @TmsLink / allure.link.tms.pattern")

    if args.dry_run:
        target = f"existing {args.cycle_key}" if args.cycle_key else f"new {args.cycle_name!r}"
        print(f"\n[dry-run] Cycle: {target}")
        print(f"[dry-run] Base URL: {args.base_url.rstrip('/')}")
        for key, st in sorted(resolved_status.items()):
            print(f"  {key:12s} -> {st} ({status_map.get(st)})")
        return

    require_live_args(args)

    token = os.environ.get("JIRA_PAT")
    if not token:
        sys.exit("Environment variable JIRA_PAT is not set")

    api = ZephyrDC(
        token=token,
        base_url=args.base_url,
        project_id=args.project_id,
        folder_id=args.folder_id,
        jira_version_id=args.jira_version_id,
        cycle_status_id=args.cycle_status_id,
        owner_key=args.owner_key,
    )

    key_to_id = api.resolve_keys(list(resolved_status.keys()))
    missing_keys = [k for k in resolved_status if k not in key_to_id]
    if missing_keys:
        print("Cases not found in Zephyr (check keys in @TmsLink):")
        for k in missing_keys:
            print(f"  {k}")

    case_ids = [key_to_id[k] for k in resolved_status if k in key_to_id]
    if not case_ids:
        sys.exit("None of the keys resolved to an id")

    if args.cycle_key:
        run_id, run_key, item_count = api.get_cycle(args.cycle_key)
        print(f"\nUsing existing Test Cycle: {run_key} (id {run_id})")
        print(f"{api.base_url}/secure/Tests.jspa#/testCycle/{run_key}")
        existing = api.fetch_result_ids(run_key)
        new_ids = [cid for cid in case_ids if cid not in existing]
        if new_ids:
            api.add_items(run_id, new_ids)
            print(f"Added new cases: {len(new_ids)} (already present: {len(existing)})")
        else:
            print(f"All {len(case_ids)} cases are already in the cycle — skip add")
    else:
        run_id, run_key = api.create_cycle(args.cycle_name, args.description)
        print(f"\nCreated Test Cycle: {run_key} (id {run_id})")
        print(f"{api.base_url}/secure/Tests.jspa#/testCycle/{run_key}")
        api.add_items(run_id, case_ids)
        print(f"Added cases: {len(case_ids)}")

    case_to_result = api.fetch_result_ids(run_key)

    ok = 0
    for key, st in resolved_status.items():
        cid = key_to_id.get(key)
        rid = case_to_result.get(cid)
        if not rid:
            print(f"  ! no result-id for {key} (id {cid}) — skip")
            continue
        if api.set_status(rid, status_map[st]):
            ok += 1

    print(f"\nStatuses set: {ok}/{len(case_ids)}")
    if missing_keys:
        sys.exit(2)


if __name__ == "__main__":
    main()
