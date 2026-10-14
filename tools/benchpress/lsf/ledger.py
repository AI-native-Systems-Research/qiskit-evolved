"""Durable intents around bsub. Only an explicit new attempt can submit new work."""

import getpass
import re
import uuid
from schemas import atomic_json, locked, read_json, utcnow
from lsf.scheduler import Unresolved


class Ledger:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "ledger.json"

    def read(self):
        return read_json(self.path) if self.path.exists() else {"schema_version": 1, "intents": []}

    def event(self, kind, **fields):
        with (self.directory / "logs/events.jsonl").open("a", encoding="utf-8") as stream:
            import json
            import os

            stream.write(
                json.dumps({"at": utcnow(), **fields, "kind": kind}, sort_keys=True) + "\n"
            )
            stream.flush()
            os.fsync(stream.fileno())

    def submit(self, key, role, attempt, indices, spec_factory, scheduler):
        with locked(self.directory / "ledger.lock"):
            data = self.read()
            intent = next((r for r in data["intents"] if r["key"] == key), None)
            if intent:
                if intent["status"] == "rejected":
                    raise RuntimeError(f"Submission {key} was rejected; use explicit resume")
                scheduler.reconcile(intent)
                atomic_json(self.path, data)
                return intent
            label = re.sub(r"[^A-Za-z0-9_-]", "_", self.directory.name[-30:])
            name = f"bp-{label}-{role}-{uuid.uuid4().hex[:10]}"
            spec = spec_factory(name)
            intent = {
                "key": key,
                "role": role,
                "attempt": attempt,
                "indices": indices,
                "name": spec.name,
                "owner": getpass.getuser(),
                "status": "intent",
                "job_id": None,
                "created": utcnow(),
                "argv": spec.argv(),
            }
            data["intents"].append(intent)
            atomic_json(self.path, data)
            self.event("submit_intent", **intent)
            result = scheduler.submit(spec)
            intent.update(status=result["outcome"], job_id=result["job_id"])
            atomic_json(self.path, data)
            self.event("submit_result", key=key, **result)
            if result["outcome"] != "accepted":
                raise Unresolved(f"{result['outcome']} submission {key}: {result['stderr']}")
            return intent

    def reconcile(self, scheduler):
        with locked(self.directory / "ledger.lock"):
            data = self.read()
            statuses = {}
            for intent in data["intents"]:
                try:
                    statuses[intent["key"]] = scheduler.reconcile(intent)
                    atomic_json(
                        self.directory / "outcomes" / f"scheduler-{intent['key']}.json",
                        statuses[intent["key"]],
                    )
                except Unresolved as exc:
                    statuses[intent["key"]] = [{"state": "UNRESOLVED", "reason": str(exc)}]
            atomic_json(self.path, data)
            atomic_json(self.directory / "scheduler-status.json", statuses)
            return statuses
