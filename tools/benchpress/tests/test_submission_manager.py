"""Source pin validation and stage sequencing with a fake scheduler."""

import json
import subprocess

import pytest
from lsf import manager
from lsf.submit import main, parser, validate
from prepare import archive
from schemas import atomic_json, file_hash, state_dir, write_jsonl


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def committed_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "harness")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Harness tests")
    (repo / "source").write_text("baseline")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "baseline")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "main", baseline)
    (repo / "source").write_text("candidate")
    git(repo, "commit", "-qam", "candidate")
    candidate = git(repo, "rev-parse", "HEAD")
    git(repo, "branch", "sabre-gate-fusion-aware-scoring", candidate)
    harness = repo / "tools/benchpress/lsf"
    harness.mkdir(parents=True)
    (harness / "submit.py").write_text("# harness\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "harness")
    return repo, baseline, candidate


def test_algorithm_pins_do_not_follow_harness_parent(tmp_path):
    repo, baseline, candidate = committed_repo(tmp_path)
    args = parser().parse_args(
        [
            "--repo",
            str(repo),
            "--results-root",
            str(tmp_path / "results"),
            "--baseline-ref",
            baseline,
            "--candidate-ref",
            candidate,
            "--dry-run",
        ]
    )
    config = validate(args)
    assert config["baseline_ref"] == baseline
    assert config["candidate_ref"] == candidate
    assert config["harness_ref"] not in (baseline, candidate)
    assert config["opt_levels"] == [2, 3]
    (repo / ".gitignore").write_text("unrelated\n")
    validate(args)
    (repo / "tools/benchpress/lsf/submit.py").write_text("changed")
    with pytest.raises(ValueError, match="differ"):
        validate(args)
    assert not args.results_root.exists()


def test_default_branch_comparison_dry_run(tmp_path, capsys):
    repo, baseline, _candidate = committed_repo(tmp_path)
    root = tmp_path / "results"
    main(["--repo", str(repo), "--results-root", str(root), "--dry-run"])
    preview = json.loads(capsys.readouterr().out)
    config = preview["configuration"]
    assert config["baseline_ref"] == baseline
    assert config["candidate_ref"] == git(repo, "rev-parse", "HEAD")
    assert config["requested_refs"] == {
        "baseline": "main",
        "candidate": "HEAD",
        "harness": "HEAD",
    }
    assert config["opt_levels"] == [2, 3]
    assert preview["array_elements"] == 150
    assert preview["invocations"] == 600
    assert not root.exists()
    assert not state_dir(root).exists()
    assert config["skip_candidate_unit_tests"] is False


def test_skip_candidate_unit_tests_is_recorded(tmp_path, capsys):
    repo, _baseline, _candidate = committed_repo(tmp_path)
    root = tmp_path / "results"
    main(
        [
            "--repo",
            str(repo),
            "--results-root",
            str(root),
            "--skip-candidate-unit-tests",
            "--dry-run",
        ]
    )
    config = json.loads(capsys.readouterr().out)["configuration"]
    assert config["skip_candidate_unit_tests"] is True


def test_diverged_branches_are_pinned_before_archiving(tmp_path):
    repo, _baseline, candidate = committed_repo(tmp_path)
    git(repo, "checkout", "-q", "main")
    (repo / "source").write_text("advanced main")
    git(repo, "commit", "-qam", "advance main independently of candidate")
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "harness")
    args = parser().parse_args(
        [
            "--repo",
            str(repo),
            "--results-root",
            str(tmp_path / "results"),
            "--candidate-ref",
            "sabre-gate-fusion-aware-scoring",
        ]
    )
    config = validate(args)
    assert config["baseline_ref"] == baseline
    assert config["candidate_ref"] == candidate
    assert config["requested_refs"]["candidate"] == "sabre-gate-fusion-aware-scoring"
    # Moving either branch after validation must not change the archived algorithm sources.
    git(repo, "branch", "-f", "main", "HEAD")
    git(repo, "branch", "-f", "sabre-gate-fusion-aware-scoring", "HEAD")
    for role, expected in (("baseline", "advanced main"), ("candidate", "candidate")):
        destination = tmp_path / role
        archive(repo, config[f"{role}_ref"], destination)
        assert (destination / "source").read_text() == expected


@pytest.mark.parametrize("ref_kind", ["remote", "tag", "short-sha"])
def test_other_commit_refs_are_supported(tmp_path, ref_kind):
    repo, _baseline, candidate = committed_repo(tmp_path)
    if ref_kind == "remote":
        ref = "origin/sabre-gate-fusion-aware-scoring"
        git(repo, "update-ref", "refs/remotes/" + ref, candidate)
    elif ref_kind == "tag":
        ref = "candidate-tag"
        git(repo, "tag", "-a", ref, candidate, "-m", "candidate tag")
    else:
        ref = candidate[:12]
    args = parser().parse_args(
        ["--repo", str(repo), "--results-root", str(tmp_path / "results"), "--candidate-ref", ref]
    )
    config = validate(args)
    assert config["candidate_ref"] == candidate
    assert config["requested_refs"]["candidate"] == ref


@pytest.mark.parametrize("role", ["baseline", "candidate", "harness"])
@pytest.mark.parametrize("ref", ["missing-branch", "HEAD^{tree}", "HEAD:source", "--all", ""])
def test_invalid_commit_refs_fail_before_creating_session(tmp_path, role, ref):
    repo, _baseline, _candidate = committed_repo(tmp_path)
    root = tmp_path / "results"
    args = parser().parse_args(
        ["--repo", str(repo), "--results-root", str(root), f"--{role}-ref={ref}"]
    )
    with pytest.raises(ValueError, match=f"--{role}-ref .* does not resolve to a commit"):
        validate(args)
    assert not root.exists()
    assert not state_dir(root).exists()


@pytest.mark.parametrize("profile", ["quality", "followup-quality", "full"])
def test_all_profiles_default_to_l2_and_l3(tmp_path, profile):
    repo, _baseline, _candidate = committed_repo(tmp_path)
    extra = []
    if profile == "followup-quality":
        panel_file = tmp_path / "panel.jsonl"
        write_jsonl(panel_file, [{"node_id": "test.py::test_case", "selection_reason": "followup"}])
        extra = ["--panel-file", str(panel_file)]
    elif profile == "full":
        extra = ["--campaign-limit-min", "180"]
    args = parser().parse_args(
        [
            "--repo",
            str(repo),
            "--results-root",
            str(tmp_path / "results"),
            "--profile",
            profile,
            *extra,
        ]
    )
    assert validate(args)["opt_levels"] == [2, 3]


@pytest.mark.parametrize(
    "extra",
    [
        ["--opt-levels", "0"],
        ["--opt-levels", "1"],
        ["--opt-levels", "1", "2", "3"],
        ["--opt-levels", "4"],
        ["--opt-levels", "2", "2"],
        ["--seeds", "-1"],
        ["--seeds", "0", "0"],
        ["--run-timeout-s", "600"],
        ["--task-wall", "0:20"],
        ["--profile", "full"],
        ["--profile", "followup-quality"],
    ],
)
def test_invalid_matrix_and_resource_options(tmp_path, extra):
    repo, baseline, candidate = committed_repo(tmp_path)
    args = parser().parse_args(
        [
            "--repo",
            str(repo),
            "--results-root",
            str(tmp_path / "results"),
            "--baseline-ref",
            baseline,
            "--candidate-ref",
            candidate,
            *extra,
        ]
    )
    with pytest.raises(ValueError):
        validate(args)


def test_manager_build_then_serial_chunks_then_report_even_after_failed_element(
    tmp_path, monkeypatch
):
    root = tmp_path / "results"
    directory = state_dir(root)
    (directory / "logs").mkdir(parents=True)
    cfg = {
        "profile": "quality",
        "results_root": str(root),
        "log_level": "INFO",
        "cluster": {"max_array_size": 2},
        "max_parallel": 16,
        "build_slots": 16,
        "build_mem_gb": 64,
        "task_mem_gb": 8,
        "queue": None,
        "project": None,
        "build_wall": "6:00",
        "manager_wall": "24:00",
        "task_wall": "0:30",
        "report_wall": "0:30",
        "lsf_memory_unit": "GB",
    }
    atomic_json(directory / "launch.json", cfg)
    tasks = [{"array_index": i, "stage": "quality"} for i in range(1, 6)]
    calls = []

    class FakeLedger:
        def __init__(self, path):
            pass

        def submit(self, key, role, epoch, indices, factory, scheduler):
            calls.append(("submit", role, indices))
            spec = factory("test")
            if role == "quality":
                assert spec.name.endswith("%16")
                assert len(indices) <= 2
            return {"key": key, "role": role, "job_id": "123"}

    def fake_wait(intent, *args, **kwargs):
        calls.append(("settled", intent["role"]))
        if intent["role"] == "build":
            atomic_json(root / "results/manifest.json", {"invocation_count": 20})
            atomic_json(
                root / "build/ready.json",
                {"manifest_hash": file_hash(root / "results/manifest.json")},
            )
            write_jsonl(root / "tasks/quality.jsonl", tasks)
        return [{"state": "EXIT" if intent["role"] == "quality" else "DONE"}]

    monkeypatch.setattr(manager, "Ledger", FakeLedger)
    monkeypatch.setattr(manager, "wait_for", fake_wait)
    monkeypatch.setattr(manager, "wrapper", lambda *args: directory / "fake.sh")
    manager.manage(root, 1)
    assert [c[1] for c in calls if c[0] == "submit"] == [
        "build",
        "quality",
        "quality",
        "quality",
        "report",
    ]
    assert all(
        calls[i][0] == "submit" and calls[i + 1][0] == "settled" for i in range(0, len(calls), 2)
    )
    assert (directory / "complete.json").exists()


def test_deadline_cancels_owned_array_and_waits_for_terminal(tmp_path):
    from lsf.manager import wait_for

    atomic_json(tmp_path / "epoch-1-started.json", {"deadline": 0})

    class FakeScheduler:
        cancelled = False

        def reconcile(self, intent):
            return [{"state": "EXIT" if self.cancelled else "PEND"}]

        def cancel(self, intent):
            assert intent["key"] == "owned-array"
            self.cancelled = True

    scheduler = FakeScheduler()
    rows = wait_for(
        {"key": "owned-array"}, scheduler, tmp_path, 1, campaign=True, sleep=lambda _: None
    )
    assert scheduler.cancelled
    assert rows[0]["state"] == "EXIT"
