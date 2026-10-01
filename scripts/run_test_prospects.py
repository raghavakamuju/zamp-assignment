"""Batch-submit test_prospects.json against a running server and print a
summary table. Usage: python scripts/run_test_prospects.py [base_url]
"""
import json
import sys
import time
from pathlib import Path
from urllib.request import Request, urlopen

BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
TERMINAL = {"ready_for_review", "flagged_no_signal", "flagged_ungrounded", "error"}


def post(path: str, body: dict) -> dict:
    req = Request(f"{BASE_URL}{path}", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urlopen(req) as resp:
        return json.load(resp)


def get(path: str) -> dict:
    with urlopen(f"{BASE_URL}{path}") as resp:
        return json.load(resp)


def main() -> None:
    prospects = json.loads(Path(__file__).resolve().parent.parent.joinpath("test_prospects.json").read_text())
    results = []
    for p in prospects:
        run = post("/runs", {"prospect_name": p["prospect_name"], "company_name": p["company_name"], "title": p["title"]})
        run_id = run["id"]
        print(f"submitted {p['company_name']:30s} -> {run_id}")
        status = "pending"
        for _ in range(30):
            detail = get(f"/runs/{run_id}")
            status = detail["status"]
            if status in TERMINAL:
                break
            time.sleep(2)
        results.append({"company": p["company_name"], "status": status, "confidence": detail.get("confidence"), "run_id": run_id})

    print("\n--- Summary ---")
    for r in results:
        print(f"{r['company']:30s} {r['status']:20s} confidence={r['confidence']}  id={r['run_id']}")


if __name__ == "__main__":
    main()
