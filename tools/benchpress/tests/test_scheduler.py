"""Submission uncertainty, history, ownership and resource boundary tests."""

import json
from types import SimpleNamespace

import pytest
from schemas import read_json
from lsf.ledger import Ledger
from lsf.scheduler import (
    JobSpec,
    Scheduler,
    Unresolved,
    parse_bjobs,
    parse_history,
    resources,
    wall_seconds,
)


def spec(name="test"):
    return JobSpec(
        name, ["/bin/bash", "/shared/worker.sh"], 1, 8, "0:30", "out", "err", exclusive=True
    )


def test_resource_requests_and_wall_limits():
    assert resources(1, 8) == "span[hosts=1] rusage[mem=8]"
    assert "mem=4096" in resources(16, 64, "MB", per_task=True)
    assert "mem=64" in resources(16, 64, per_task=False)
    assert "exclusive=(core,alljobs)" in " ".join(spec().argv())
    assert wall_seconds("0:30") == 1800
    assert wall_seconds("24:00") == 86400
    with pytest.raises(ValueError):
        wall_seconds("0:75")


def test_array_response_and_history_identity():
    rows = parse_bjobs(
        json.dumps(
            {
                "RECORDS": [
                    {
                        "JOBID": "123",
                        "JOB_INDEX": "2",
                        "STAT": "PEND",
                        "JOB_NAME": "test[2]",
                        "USER": "me",
                    }
                ]
            }
        )
    )
    assert rows[0]["job_id"] == "123[2]"
    assert rows[0]["state"] == "PEND"
    row = parse_history(
        "123[2]", "Job <123[2]>, Job Name <test[2]>, User <me>\nExited with exit code 137"
    )
    assert row["state"] == "EXIT"
    with pytest.raises(Unresolved):
        parse_history("123[2]", "No matching job found")
    with pytest.raises(Unresolved):
        parse_bjobs("{}")


class FakeCommands:
    def __init__(self):
        self.calls = []
        self.responses = []

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        if not self.responses:
            raise AssertionError(f"Unexpected command {argv}")
        code, stdout, stderr = self.responses.pop(0)
        return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


def test_ambiguous_submission_is_durable_and_never_resubmitted(tmp_path):
    (tmp_path / "logs").mkdir()
    commands = FakeCommands()
    commands.responses = [(0, "unrecognized response", "")]
    scheduler = Scheduler(tmp_path, commands)
    ledger = Ledger(tmp_path)
    with pytest.raises(Unresolved):
        ledger.submit("quality-1", "quality", 1, [1], spec, scheduler)
    intent = read_json(tmp_path / "ledger.json")["intents"][0]
    assert intent["status"] == "ambiguous"
    commands.responses = [(0, '{"RECORDS": []}', "")]
    with pytest.raises(Unresolved):
        ledger.submit("quality-1", "quality", 1, [1], spec, scheduler)
    assert len([c for c in commands.calls if c[0] == "bsub"]) == 1


def test_missing_job_requires_terminal_history(tmp_path):
    commands = FakeCommands()
    commands.responses = [
        (0, '{"RECORDS": []}', ""),
        (0, "Job <123>, Job Name <test>, User <me>\nStill running", ""),
    ]
    intent = {"job_id": "123", "name": "test", "owner": "me", "indices": [], "status": "accepted"}
    rows = Scheduler(tmp_path, commands).reconcile(intent)
    assert rows[0]["state"] == "UNKWN"


def test_failed_query_and_wrong_owner_never_cancel(tmp_path):
    commands = FakeCommands()
    commands.responses = [(1, "", "scheduler unavailable")]
    intent = {"job_id": "123", "name": "test", "owner": "me", "indices": [], "status": "accepted"}
    scheduler = Scheduler(tmp_path, commands)
    with pytest.raises(Unresolved):
        scheduler.cancel(intent)
    commands.responses = [
        (
            0,
            json.dumps(
                {
                    "RECORDS": [
                        {"JOBID": "123", "STAT": "RUN", "JOB_NAME": "test", "USER": "someone-else"}
                    ]
                }
            ),
            "",
        )
    ]
    with pytest.raises(Unresolved):
        scheduler.cancel(intent)
    assert all(c[0] != "bkill" for c in commands.calls)


def test_cancel_only_named_owned_nonterminal_jobs(tmp_path):
    commands = FakeCommands()
    row = {"JOBID": "123[1]", "STAT": "RUN", "JOB_NAME": "test[1]", "USER": "me"}
    commands.responses = [
        (0, json.dumps({"RECORDS": [row]}), ""),
        (0, "terminated", ""),
        (0, json.dumps({"RECORDS": [dict(row, STAT="EXIT")]}), ""),
    ]
    intent = {
        "job_id": "123",
        "name": "test[1-1]%16",
        "owner": "me",
        "indices": [1],
        "status": "accepted",
    }
    rows = Scheduler(tmp_path, commands).cancel(intent)
    assert rows[0]["state"] == "EXIT"
    assert [c for c in commands.calls if c[0] == "bkill"] == [["bkill", "123[1]"]]


def test_confirmed_terminal_evidence_survives_history_expiry(tmp_path):
    from schemas import atomic_json

    intent = {
        "key": "quality-1",
        "job_id": "123",
        "name": "test[1-1]%16",
        "owner": "me",
        "indices": [1],
        "status": "accepted",
    }
    atomic_json(
        tmp_path / "outcomes/scheduler-quality-1.json",
        [{"job_id": "123[1]", "name": "test[1]", "owner": "me", "state": "DONE"}],
    )
    commands = FakeCommands()
    rows = Scheduler(tmp_path, commands).reconcile(intent)
    assert rows[0]["state"] == "DONE"
    assert commands.calls == []


def test_cluster_units_and_multislot_reservation(tmp_path):
    commands = FakeCommands()
    commands.responses = [
        (0, "LSF_UNIT_FOR_LIMITS=GB", ""),
        (0, "MAX_JOB_ARRAY_SIZE=1000\nRESOURCE_RESERVE_PER_TASK=Y", ""),
    ]
    config = Scheduler(tmp_path, commands).cluster_config("GB")
    assert config == {"memory_unit": "GB", "max_array_size": 1000, "reserve_per_task": True}
