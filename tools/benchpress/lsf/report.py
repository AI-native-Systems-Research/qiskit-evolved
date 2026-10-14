"""Operational status separate from scientific quality tables."""

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
        "Use `control.py status` for current scheduler states. No compilation runtime is measured.",
    ]
    atomic_text(directory / "report.md", "\n".join(lines) + "\n")
