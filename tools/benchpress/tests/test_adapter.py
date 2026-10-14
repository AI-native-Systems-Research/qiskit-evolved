"""Patch insertion must handle positional/keyword calls without adding extra calls."""

import ast

import pytest
from adapter import seed_calls, replace_once


@pytest.mark.parametrize(
    "call",
    [
        "generate_preset_pass_manager(LEVEL, BACKEND)",
        "generate_preset_pass_manager(optimization_level=LEVEL, backend=backend\n)",
        "generate_preset_pass_manager(optimization_level=LEVEL, backend=backend,\n)",
    ],
)
def test_seed_patch_in_all_call_forms(call):
    source = (
        "from qiskit.transpiler.preset_passmanagers import generate_preset_pass_manager\npm = "
        + call
    )
    patched = seed_calls(source)
    calls = [
        n
        for n in ast.walk(ast.parse(patched))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "generate_preset_pass_manager"
    ]
    assert len(calls) == 1
    assert sum(k.arg == "seed_transpiler" for k in calls[0].keywords) == 1
    assert "from bp_runtime import" in patched
    with pytest.raises(ValueError):
        seed_calls(patched)


def test_adapter_refuses_source_drift():
    with pytest.raises(ValueError):
        replace_once("source drift", "expected", "new")
