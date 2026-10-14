"""Operational status separate from scientific quality and runtime tables."""

from pathlib import Path
from schemas import atomic_text, read_json, state_dir
from lsf.ledger import Ledger


def operational_report(root):
    root = Path(root)
    directory = state_dir(root)
    rows = Ledger(directory).read()["intents"]
    lines = [
        "# LSF campaign status",
        "",
        "| Stage | Attempt | Job ID | Submission |",
        "| --- | ---: | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['role']} | {row['attempt']} | {row.get('job_id') or 'unresolved'} | {row['status']} |"
        )
    if (directory / "attention.json").exists():
        lines += ["", "Attention: " + read_json(directory / "attention.json")["reason"]]
    lines += [
        "",
        f"Scientific report: `{root / 'results/report.md'}`.",
        "",
        "Scheduler responses are preserved in `diagnostics/`; child outcomes in `outcomes/`.",
        "Use `control.py status` for current scheduler states. Callable runtime comparisons and timing coverage are in the scientific report.",
    ]
    summary_path = root / "results/summary.json"
    if summary_path.exists():
        summary = read_json(summary_path)
        lines += [
            "",
            f"Quality coverage: {summary['paired']}/{summary['pairs']} paired seeds; {summary['complete_cases']}/{summary['cases']} complete cases.",
            f"Runtime coverage: {summary.get('runtime_paired', 0)}/{summary['pairs']} paired seeds; {summary.get('runtime_complete_cases', 0)}/{summary['cases']} complete cases.",
            "Runtime uses single-call observations without warmup or calibration; fixed ordering and host load can affect the measurements.",
        ]
    atomic_text(directory / "report.md", "\n".join(lines) + "\n")
