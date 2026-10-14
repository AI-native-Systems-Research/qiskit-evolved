"""Build, submit serial array chunks with one global cap, then report all outcomes."""

import argparse
import logging
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from schemas import atomic_json, locked, read_json, read_jsonl, state_dir, utcnow
from lsf.ledger import Ledger
from lsf.scheduler import Scheduler, TERMINAL, Unresolved
from lsf.submit import job_spec, wrapper


def wait_for(intent, scheduler, directory, epoch, campaign=False, sleep=time.sleep):
    while True:
        rows = scheduler.reconcile(intent)
        atomic_json(directory / "outcomes" / f"scheduler-{intent['key']}.json", rows)
        if all(r["state"] in TERMINAL for r in rows):
            return rows
        if any(
            r["state"] not in TERMINAL | {"PEND", "RUN", "PSUSP", "USUSP", "SSUSP", "WAIT", "PROV"}
            for r in rows
        ):
            raise Unresolved(f"Nonterminal unknown state in {intent['key']}")
        expired = False
        marker = directory / f"epoch-{epoch}-started.json"
        if campaign and marker.exists():
            expired = time.time() >= read_json(marker)["deadline"]
        if ((directory / "stop.json").exists() and intent.get("role") != "report") or expired:
            scheduler.cancel(intent)
        sleep(15)


def unfinished(root, tasks, epoch):
    directory = state_dir(root)
    indices = []
    for task in tasks:
        outcomes = [
            directory / "outcomes" / f"{task['stage']}-{task['array_index']}-{attempt}.json"
            for attempt in range(1, epoch)
        ]
        complete = any(path.exists() and read_json(path)["status"] == "passed" for path in outcomes)
        if not complete:
            indices.append(task["array_index"])
    return indices


def manage(root, epoch):
    root = Path(root)
    directory = state_dir(root)
    config = read_json(directory / "launch.json")
    logging.basicConfig(
        filename=directory / "logs" / f"manager-{epoch}.log",
        level=config["log_level"],
        format="%(asctime)s %(levelname)s %(message)s",
    )
    scheduler, ledger = Scheduler(directory), Ledger(directory)
    # A resumed manager can dispatch before the launcher releases this lock.
    with locked(directory / "manager.lock"):
        try:
            if not (root / "build/ready.json").exists():
                script = wrapper(config, "build", epoch)
                intent = ledger.submit(
                    f"build-{epoch}",
                    "build",
                    epoch,
                    [],
                    lambda name: job_spec(config, "build", name, script),
                    scheduler,
                )
                logging.info("Waiting for build job %s", intent["job_id"])
                rows = wait_for(intent, scheduler, directory, epoch)
                if (
                    any(r["state"] != "DONE" for r in rows)
                    or not (root / "build/ready.json").exists()
                ):
                    raise RuntimeError("Build/validation failed; no quality tasks were submitted")
            from schemas import manifest

            frozen = manifest(root, read_json(root / "build/ready.json")["manifest_hash"])
            tasks = read_jsonl(root / "tasks" / f"{config['profile']}.jsonl")
            indices = unfinished(root, tasks, epoch)
            atomic_json(
                directory / f"attempt-{epoch}.json",
                {
                    "attempt": epoch,
                    "indices": indices,
                    "selection_rule": "rerun every child of an incomplete element; preserve canonical attempt 1",
                },
            )
            chunk_size = config["cluster"]["max_array_size"]
            # Serial chunks enforce a campaign-wide cap, including full-corpus arrays.
            for chunk, offset in enumerate(range(0, len(indices), chunk_size), 1):
                marker = directory / f"epoch-{epoch}-started.json"
                if (directory / "stop.json").exists() or (
                    marker.exists() and time.time() >= read_json(marker)["deadline"]
                ):
                    break
                members = indices[offset : offset + chunk_size]
                chunk_path = directory / f"chunk-{epoch}-{chunk}.json"
                atomic_json(
                    chunk_path,
                    {
                        "indices": members,
                        "manifest_hash": read_json(root / "build/ready.json")["manifest_hash"],
                    },
                )
                script = wrapper(config, "quality", epoch, chunk)
                intent = ledger.submit(
                    f"quality-{epoch}-{chunk}",
                    "quality",
                    epoch,
                    list(range(1, len(members) + 1)),
                    lambda name: job_spec(config, "quality", name, script, members),
                    scheduler,
                )
                logging.info(
                    "Waiting for quality array %s (%s tasks)", intent["job_id"], len(members)
                )
                wait_for(intent, scheduler, directory, epoch, campaign=True)
            # All submitted children are confirmed terminal, including EXIT elements.
            script = wrapper(config, "report", epoch)
            intent = ledger.submit(
                f"report-{epoch}",
                "report",
                epoch,
                [],
                lambda name: job_spec(config, "report", name, script),
                scheduler,
            )
            rows = wait_for(intent, scheduler, directory, epoch)
            if any(r["state"] != "DONE" for r in rows):
                raise RuntimeError("Reporter failed; raw artifacts are preserved")
            atomic_json(
                directory / "complete.json",
                {
                    "epoch": epoch,
                    "completed": utcnow(),
                    "report": str(root / "results/report.md"),
                    "invocations": frozen["invocation_count"],
                },
            )
            logging.info("Report complete: %s", root / "results/report.md")
        except Exception as exc:
            logging.exception("Manager stopped: %s", exc)
            atomic_json(
                directory / "attention.json", {"epoch": epoch, "at": utcnow(), "reason": str(exc)}
            )
            # Do not assume uncertain jobs are dead, retry, or publish a scientific conclusion.
            from lsf.report import operational_report

            operational_report(root)
            raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-root", required=True, type=Path)
    parser.add_argument("--epoch", required=True, type=int)
    args = parser.parse_args()
    manage(args.results_root, args.epoch)
