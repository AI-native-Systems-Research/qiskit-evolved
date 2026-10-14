"""Inspect or cancel only jobs whose ID, unique name, and owner match this session."""

import argparse
import getpass
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schemas import atomic_json, read_json, state_dir, utcnow
from lsf.ledger import Ledger
from lsf.scheduler import Scheduler, TERMINAL


def control(root, action):
    directory = state_dir(root)
    config = read_json(directory / "launch.json")
    scheduler, ledger = Scheduler(directory), Ledger(directory)
    if action in ("stop", "reap"):
        if config["owner"] != getpass.getuser():
            raise ValueError("Only the recorded session owner may cancel its jobs")
        atomic_json(directory / "stop.json", {"requested": utcnow(), "action": action})
        # A stopped manager must not race a new stage submission with cancellation.
        # First cancel managers; then reread the ledger until stable and terminal.
        for managers in (True, False):
            for intent in ledger.read()["intents"]:
                if (intent["role"] == "manager") == managers and intent["status"] != "rejected":
                    scheduler.cancel(intent)
        deadline = time.monotonic() + 45
        while True:
            statuses = ledger.reconcile(scheduler)
            if all(r["state"] in TERMINAL for rows in statuses.values() for r in rows):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    "Cancellation requested but termination is unresolved; rerun status/reap"
                )
            for intent in ledger.read()["intents"]:
                if intent["status"] != "rejected":
                    scheduler.cancel(intent)
            time.sleep(2)
    statuses = ledger.reconcile(scheduler)
    result = {
        "jobs": statuses,
        "report_exists": (Path(root) / "results/report.md").exists(),
        "complete": (directory / "complete.json").exists(),
    }
    if (directory / "attention.json").exists():
        result["attention"] = read_json(directory / "attention.json")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "stop", "reap"))
    parser.add_argument("--results-root", type=Path, required=True)
    args = parser.parse_args()
    control(args.results_root.resolve(), args.action)
