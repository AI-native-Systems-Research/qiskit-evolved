# Review of commit 90ccaac29 "Include gate fusion and wire wait time in Sabre swap scoring"

Reviewed 2026-10-10 against the parent commit 1298f960b. The change was built in a scratch
copy (release profile, macOS arm64, Python 3.11.8) and the six touched test modules plus a few
targeted experiments were run; see [Verification](#verification) at the end.

## Verdict

**No critical bug.** Routing output is always a valid circuit whatever the scores are, and the new
state (`ready`, `last_block`, `ready_undo`) is maintained and unwound consistently. Nothing here
can produce a wrong circuit, a panic, or a hang that did not exist before.

**Two high-severity quality problems** should be fixed before this is merged, because in common
configurations the change makes the final native two-qubit gate count *worse* than before:

1. The fusion credit assumes a two-qubit resynthesis pass that only runs at optimization levels 2
   and 3. At levels 0 and 1, and in any custom pipeline, the extra swaps it buys are paid in full.
2. The block model (`block_gates`) disagrees with the predicate that decides what actually gets
   resynthesised (`DAGCircuit::collect_2q_runs`). Parameterized circuits, the main case, get the
   credit but never the fusion.

The rest are medium or low: the commit fails the project's `cargo clippy -D warnings` lint (three
diagnostics, all in the new code), the trial/layout selection criterion was not updated, the wait
term is in absolute units, the new constants are not exposed through `Heuristic`, docs and a
release note are missing, and one re-pinned test fails on this machine.

## What the commit does

Two new terms are added to the per-candidate swap score in `State::choose_best_swap`
([route.rs:963](../crates/transpiler/src/passes/sabre/route.rs:963)):

- **Fusion credit.** `SabreDAG` now estimates, per two-qubit node, how many native two-qubit
  gates it contributes to a fusible block on its pair (`SabreNode::block`, computed by
  `block_gates` at [dag.rs:94](../crates/transpiler/src/passes/sabre/dag.rs:94)). The router keeps
  `last_block[phys]`, the partner and size `c` of the block routed last on each physical wire. A
  candidate swap on that same pair starts with score `-(3 - (min(3, c+1) - c)) / 3`, i.e. `-2/3`
  for `c` in {1, 2} and `-1` for `c = 3`.
- **Wire wait.** `ready[phys]` is an as-soon-as-possible estimate of when each wire is free (a
  routed two-qubit node adds 1 layer, a swap adds `SWAP_DEPTH = 3`). Each candidate pays
  `READY_WEIGHT * max(ready[a], ready[b])` with `READY_WEIGHT = 2.2e-5`.

A journal `ready_undo` lets the release valve restore `ready` and `last_block` when it unwinds
swaps. About twenty pinned expectations in five test files were updated and three new tests added.

## Findings

Severity scale: **Critical** = wrong output or crash; **High** = user-visible quality regression
in a supported configuration; **Medium** = design or API problem that should be fixed before
merge; **Low** = improvement or documentation.

### 1. High: the fusion credit only pays off at optimization levels 2 and 3

The credit assumes that a swap emitted right after a two-qubit block on the same pair will be
resynthesised together with it. In this checkout that only happens through
`TwoQubitPeepholeOptimization`, which the preset optimization stage runs at levels 2 and 3
([builtin_plugins.py:552-582](../qiskit/transpiler/preset_passmanagers/builtin_plugins.py:552)).
Level 1 runs only `Optimize1qGatesDecomposition`, `InverseCancellation` and
`ContractIdleWiresInControlFlow` ([builtin_plugins.py:541](../qiskit/transpiler/preset_passmanagers/builtin_plugins.py:541));
level 0 runs nothing. The `ConsolidateBlocks` in the init stage runs *before* layout and routing,
so it never sees routing swaps. Standalone `SabreSwap`/`SabreLayout` users get no resynthesis at
all unless they add it themselves.

The router cannot know which pipeline it is in, so at levels 0 and 1 the credit simply buys extra
swaps. Measured on the `test_lookahead_mode` circuit (line 0-1-2-3-4, basis `cx`/`u`, fixed
trivial layout; the pre-change router inserted one swap, this commit inserts two):

| Routing | `transpile` level 0 | level 1 | level 2/3 pipeline (`ConsolidateBlocks` + `UnitarySynthesis`) |
| --- | --- | --- | --- |
| One swap (pre-change choice) | 9 CX | 9 CX | 9 CX |
| Two swaps (this commit) | **12 CX** | **10 CX** | 8 CX |

The rationale's own breadth survey already shows small circuits (`multiplier_n16`, `mcx_n5`) above
the 1.05 N2 cap because of the credit, and that survey *includes* resynthesis.

**Fix.** Make the credit a heuristic option rather than an unconditional constant, and let the
preset pass managers enable it only when the optimization stage will resynthesise (levels 2 and
3). Concretely: add `fusion_credit: bool` (or a weight) to `Heuristic` with a `with_fusion_credit()`
builder, default off in `SabreSwap`'s named modes and in `SabreLayout`, and set it from
`builtin_plugins.py` where `TwoQubitPeepholeOptimization` is scheduled. If the authors prefer it
on by default, the release note must say that levels 0 and 1 and standalone routing can get more
native gates than before, and the level-0/1 regression should be measured on the benchmark panel.

### 2. High: `block_gates` does not match what `collect_2q_runs` will fuse

`TwoQubitPeepholeOptimization` and `ConsolidateBlocks` both collect blocks with
`DAGCircuit::collect_2q_runs` ([dag_circuit.rs:5933](../crates/circuit/src/dag_circuit.rs:5933)),
whose membership predicate is:

- a `StandardGate` on at most 2 qubits **that is not parameterized** (no unbound `Parameter`), or
- a `PyCustom` operation whose `kind` is `PyOpKind::Gate` on at most 2 qubits, not parameterized, or
- a `UnitaryGate` on at most 2 qubits;
- everything else ends the run.

`block_gates` ([dag.rs:94](../crates/transpiler/src/passes/sabre/dag.rs:94)) instead returns
`None` only for control flow, standard instructions, `Store` and Pauli-product measurements, and
treats every other op on one or two qubits as part of a block. The disagreements:

| Operation | `block_gates` | Reality | Effect |
| --- | --- | --- | --- |
| Any gate with an unbound `Parameter` (e.g. `rz(θ)`, `rzz(γ)`) | `Some(0)` / `Some(3)` | ends the run | credit granted, fusion never happens |
| `PyCustom` with `kind` `Instruction`/`Operation` (custom instructions, `Initialize`, custom directives) | `Some(0)` / `Some(3)` | ends the run | same |
| `PauliProductRotation`, `CustomOperation` on 2 qubits | `Some(3)` | ends the run | same |

The parameterized case is the important one: transpiling a parameterized ansatz once and binding
afterwards is a mainstream workflow, and `rz(θ)`/`rzz(γ)` are exactly what QAOA and VQE circuits
are made of. Measured on the `test_lookahead_mode` circuit with one extra `rz` on qubit 2 right
after `cx(1, 2)`, routed with `SabreSwap(..., "lookahead", trials=1)` and then
`ConsolidateBlocks` + `UnitarySynthesis` in the `cx`/`u` basis:

| Variant | Swaps chosen | CX after resynthesis | One-swap alternative |
| --- | --- | --- | --- |
| no `rz` | (2,1), (2,3) | 8 | 9 |
| `rz(0.1)` (bound) | (2,1), (2,3) | 8 | 9 |
| `rz(θ)` (unbound) | (2,1), (2,3) | **10** | 9 |

The router makes the same choice in all three cases because `block_gates` cannot see the
parameter, and in the unbound case the result is worse than the pre-change routing.

**Fix.** Give `block_gates` the `PackedInstruction` (it needs `is_parameterized()`) and mirror the
`collect_2q_runs` predicate. Better still, factor that predicate out of `collect_2q_runs` into a
shared `pub fn is_2q_block_member(inst: &PackedInstruction) -> bool` in `dag_circuit.rs` and call it
from both places so they cannot drift again.

```rust
use qiskit_circuit::operations::{OperationRef, PyInstruction, PyOpKind, StandardGate};
use qiskit_circuit::packed_instruction::PackedInstruction;

fn block_gates(inst: &PackedInstruction, qargs: &[Qubit]) -> Option<u8> {
    if inst.is_parameterized() {
        return None;
    }
    let fusible = match inst.op.view() {
        OperationRef::StandardGate(gate) => gate.num_qubits() <= 2,
        OperationRef::PyCustom(PyInstruction { kind: PyOpKind::Gate, qubits: ..=2, .. }) => true,
        OperationRef::Unitary(gate) => gate.num_qubits() <= 2,
        _ => false,
    };
    if !fusible {
        return None;
    }
    match (qargs, inst.op.try_standard_gate()) {
        ([_], _) => Some(0),
        ([_, _], Some(StandardGate::CX | StandardGate::CY | StandardGate::CZ
                      | StandardGate::CH | StandardGate::ECR)) => Some(1),
        ([_, _], Some(StandardGate::ISwap | StandardGate::DCX | StandardGate::CPhase
                      | StandardGate::CRX | StandardGate::CRY | StandardGate::CRZ
                      | StandardGate::CS | StandardGate::CSdg | StandardGate::CSX
                      | StandardGate::CU | StandardGate::CU1 | StandardGate::CU3
                      | StandardGate::RXX | StandardGate::RYY | StandardGate::RZZ
                      | StandardGate::RZX | StandardGate::XXMinusYY
                      | StandardGate::XXPlusYY)) => Some(2),
        ([_, _], _) => Some(3),
        _ => None,
    }
}
```

(The `Some(2)` bucket is finding 7; the first two guards are this finding.) The call site in
`from_dag` becomes `block_gates(inst, dag.get_qargs(inst.qubits))`.

### 3. Medium: trials and layouts are still selected by raw swap count

`swap_map` picks the trial with the fewest top-level swaps
([route.rs:1144](../crates/transpiler/src/passes/sabre/route.rs:1144)), and `SabreLayout` picks
the layout trial the same way ([layout.rs:203](../crates/transpiler/src/passes/sabre/layout.rs:203)
and [layout.rs:309](../crates/transpiler/src/passes/sabre/layout.rs:309)). The new per-step rule deliberately inserts *more* swaps when they
are cheaper after fusion or shallower in the schedule (the updated `test_lookahead_mode` is
exactly that: two swaps instead of one). With 5 to 20 trials, a trial that followed the new rule
can lose to one that happened not to, and the layout search prefers layouts that minimise a
quantity the router no longer minimises. The objective is now inconsistent between the two
levels of the algorithm, which also makes the measured benefit smaller than it could be.

**Fix.** Track per trial what the heuristic actually optimises and select on it. The router already
has everything needed: accumulate `native_cost += 3 - credit_gates` for each inserted swap (the
credit is known when the swap is chosen; forced swaps cost 3) and keep `max(ready)` as the
estimated depth. Then select with
`min_by_key(|(index, result)| (result.native_cost, result.max_ready, *index))`, or a documented
weighted combination. Keep the trial index as the final tie-break so results stay deterministic.
`RoutingResult`/`Order` would carry the two numbers; `rebuild` is unaffected.

### 4. Medium: the wait term is in absolute units and unbounded

The term `READY_WEIGHT * max(ready)` is added after all weighting
([route.rs:1044](../crates/transpiler/src/passes/sabre/route.rs:1044)), so its size relative to a
distance step depends on the basic weight:

- `SabreSwap(heuristic="basic")` uses `with_basic(1.0, SetScaling::Size)`, so one distance step is
  worth `1/|F|`. With 50 gates in the front layer a step is 0.02 and a 1 000-layer gap between two
  candidates' wires is 0.022: the wait term outweighs real routing progress.
- Custom `Heuristic` objects with small basic weights hit the same wall sooner.
- In the default `lookahead`/`decay` modes the crossover is about 45 000 layers. `ready` only ever
  grows, idle ancillas stay at 0, and in a very deep, very parallel circuit the gap between
  candidate wires can reach that. Past it the router prefers sideways swaps on idle wires over
  the shortest-path step and relies on the release valve to make progress.

The rationale's "never outweighs a real distance difference" holds only for the default modes and
for circuits a few thousand layers deep.

**Fix.** Put the term on the same scale as the rest of the score and bound it:

```rust
// in choose_best_swap, after the decay block
let basic_weight = /* the effective basic weight computed above, or 1.0 if basic is None */;
let t_min = self.swap_scores.iter()
    .map(|(s, _)| self.ready[s[0]].max(self.ready[s[1]])).min().unwrap_or(0);
for &(swap, score) in self.swap_scores.iter() {
    let wait = f64::from(self.ready[swap[0]].max(self.ready[swap[1]]) - t_min);
    let score = score + (basic_weight * READY_WEIGHT * wait).min(basic_weight * WAIT_CAP);
    ...
}
```

with `WAIT_CAP` well below one distance unit (0.5, say). Subtracting `t_min` does not change the
argmin of a linear term but makes the cap meaningful. Ideally `READY_WEIGHT` becomes a field of
`Heuristic` (finding 5) so the benchmark-tuned constant is not baked into every pipeline.

### 5. Medium: hard-coded constants, stale docstrings, no release note

- `READY_WEIGHT`, `SWAP_DEPTH` and the credit are constants in `route.rs`. `Heuristic` is the
  documented, pickled, Python-visible way to configure Sabre scoring, and the new
  `test_wire_ready_time_is_priced_against_lookahead` already has to build a custom `Heuristic` to
  make the term observable; there is no way to turn either term off or to reproduce pre-change
  routing. Add `with_fusion_credit()` and `with_wire_wait(weight)` builders (fields on
  `Heuristic`, included in `__repr__`, `__getnewargs__` and `__eq__`), and have the named modes
  in `sabre_swap.py` and `sabre_layout.py` set them explicitly.
- The `SabreSwap.__init__` docstring still presents the `basic`/`lookahead`/`decay` formulas as the
  complete score. They are no longer, in any mode.
- There is no `releasenotes/notes/*.yaml`. Every routed circuit changes (the commit re-pins about
  twenty expectations), so an `upgrade` note is required by the project's contribution policy,
  and a `features` note should describe the new terms and how to disable them.

### 6. Medium: one re-pinned test fails on this machine

`test_preset_passmanagers.TestFinalLayouts.test_layout_tokyo_fully_connected_cx` at optimization
level 3 expects `[5, 11, 6, 10, 0]` and produces `[5, 6, 11, 10, 0]` (macOS arm64, Python 3.11.8,
release build; reproduced today, 829 tests run, 1 failure, 3 skipped). The parent commit passes
the same test with its old pin in the same environment, so the environment alone does not explain
it. At level 3 the final layout is chosen by `VF2PostLayout` after Sabre, from the circuit's
estimated error, and the produced layout scores clearly better than the pinned one, so the pin was
most likely recorded from a different build or platform. The rationale's "826 tests, 0 failures"
is therefore not reproducible, and it names `test_mapping` and `test_stochastic*`, which do not
exist in this checkout.

**Fix.** Re-run the pin on the CI platform before merging. For the level-3 row, prefer asserting a
property that `VF2PostLayout` guarantees (the five chosen physical qubits form a connected
subgraph that routes the circuit without swaps, and the layout differs from the trivial one) over
an exact list, since the exact list depends on error-rate ties in `GenericBackendV2`.

### 6b. Medium: the commit does not pass the repository's clippy lint

`cargo clippy --all-targets -- -D warnings` (the command in `Makefile`, `tox.ini` and the lint
workflow) fails on this commit with three diagnostics, all in the new code:

- `type_complexity` at [route.rs:604](../crates/transpiler/src/passes/sabre/route.rs:604) for
  `ready_undo: Vec<(PhysicalQubit, u32, Option<(PhysicalQubit, u8)>)>`.
- `collapsible_if` twice at [route.rs:981-982](../crates/transpiler/src/passes/sabre/route.rs:981)
  for the nested `if let` / `if` / `if let` that applies the credit.

CI's lint job would reject the commit as is. Both are mechanical to fix, and the second one makes
the credit lookup easier to read:

```rust
/// One journal entry of `State::ready_undo`.
struct ReadyUndo {
    qubit: PhysicalQubit,
    ready: u32,
    block: Option<(PhysicalQubit, u8)>,
}
// ...
ready_undo: Vec<ReadyUndo>,
```

```rust
if let Some((partner, c)) = self.last_block[phys]
    && self.last_block[partner].is_some_and(|(q, _)| q == phys)
    && let Some((_, credit)) = self.swap_scores[start..]
        .iter_mut()
        .find(|(swap, _)| swap[1] == partner)
{
    *credit = f64::from(3 - ((c + 1).min(3) - c)) / -3.0;
}
```

(`let` chains are already used elsewhere in this file, so the MSRV allows them.)

### 7. Low: 2-CX-class gates are costed as 3 native gates

`block_gates` scores only `CX`, `CY`, `CZ` and `ECR` as one native gate and everything else on two
qubits as three. `CH` is also one-CX-equivalent, and `iSwap`, `DCX`, `CPhase`, `CRX/CRY/CRZ`,
`CS/CSdg/CSX`, `CU/CU1/CU3`, `RXX/RYY/RZZ/RZX` and `XX±YY` need two CX for generic angles. With
`c = 3` the model says a swap after an `rzz` block is free (credit `-1`, which ties with a real
distance step), whereas the true saving is two of three gates (credit `-2/3`). The rationale reports
QAOA, an `rzz`-heavy workload, as the one case whose N2 rises (+4.6 %) and as the binding
constraint on `READY_WEIGHT`; the over-credit is a plausible contributor and is cheap to test with
the `Some(2)` bucket shown in finding 2.

### 8. Low: layout-search passes use a looser block model than final routing

`only_interactions` ([dag.rs:261](../crates/transpiler/src/passes/sabre/dag.rs:261)) keeps
`block` but drops `indices`. `advance_routed` ends blocks for non-two-qubit nodes by reading the
original instructions through `indices`
([route.rs:745-752](../crates/transpiler/src/passes/sabre/route.rs:745)), so in `layout_trial`'s
forward/backward passes barriers, measurements, resets, 3q+ gates and control-flow nodes never end
a block and credits are more generous than the final routing will see. The guide documents this;
the code should not need the caveat.

**Fix.** Record the touched virtual qubits on the node at `from_dag` time, e.g.
`SabreNode::ends_block: Box<[VirtualQubit]>` for `Synchronize`/`ControlFlow` nodes (empty for
`TwoQ`), keep it in `only_interactions`, and have `advance_routed` clear `last_block` from it.
This also removes a DAG lookup per routed synchronisation node.

### 9. Low: the depth model is internally inconsistent

- `advance_swap` always adds `SWAP_DEPTH = 3`, even when the same swap was just credited as fusing
  into a block of `c` gates and will cost `min(3, c+1) - c` extra gates (0 to 1). The credit and
  the schedule disagree about the same swap.
- A two-qubit node adds one layer regardless of how many two-qubit gates it folds (`block` is
  capped at 3, but a node can hold a long run), so chains of same-pair gates are nearly invisible
  to `ready`.
- Measurements, resets, 3q+ gates, control-flow nodes and the control-flow epilogue swaps add no
  time, and inner control-flow routing starts from `ready = 0`.

These are estimates, so none is a bug, but the first one is trivial to fix: compute the increment
alongside the credit (`fused_cost` instead of `SWAP_DEPTH`) and record it in `ready_undo` as now.
For the second, `node.block.unwrap_or(1).max(1)` is a better per-node increment than 1.

### 10. Low: the credit is as large as a full distance step

With `c = 3` a candidate that makes no routing progress (`ΔF = 0`) scores `-1`, the same as the
shortest-path step. Under the model such a swap is free, but it changes the final permutation,
raises the swap count (finding 3), adds three layers to `ready` (finding 9) and depends on
findings 1 and 2 to be realised. Consider scaling the credit by a factor below one, or applying it
only to candidates with `ΔF <= 0`, so that it breaks ties among useful swaps rather than
creating sideways moves.

### 11. Low: documentation accuracy

- `RATIONALE.md`: "Nothing new is tracked or allocated" is true only relative to the previous
  attempt; relative to `main` the change adds three per-qubit arrays and an undo journal.
  "826 tests, 0 failures" and the module names are wrong (finding 6).
- `InteractionKind::ControlFlow`'s doc comment (pre-existing) says 2q control flow is handled as
  `TwoQ`; the code always builds `ControlFlow`.
- The new `SabreNode::block` field doc says "or `None` if it contains anything ending the block";
  after finding 2 it should list what ends a block, by reference to `collect_2q_runs`.

## Things checked and found correct

- **Undo journal.** Entries are pushed in chronological order and popped LIFO, so each qubit ends
  on its oldest saved value; both `ready` and `last_block` are restored; the journal is cleared per
  search step; forced swaps re-advance after the restore and are never unwound (correct, they are
  kept).
- **Credit lookup.** Each undirected edge is pushed once (`neighbor > phys` or inactive
  neighbour). When `partner < phys` and both are active the credit is found from the partner's
  iteration; when the partner is inactive it is found from `phys`. No pair gets the credit twice,
  and the mutual `last_block` check guarantees both wires' last operation was the same block.
- **A credited pair is never the front gate's own pair.** `last_block` pairs are adjacent (they
  were routed as a gate), and an adjacent front-layer pair would already have been routed.
- **Arithmetic.** `3 - ((c+1).min(3) - c)` cannot underflow for `c <= 3`; `block` is capped at 3 in
  both `from_dag` and `advance_routed`; `f64::from(u32)` is exact.
- **Ordering.** `ready` and `last_block` are indexed by physical qubit, so `apply_swap` before or
  after `advance_swap` makes no difference. `advance_routed` is only called for nodes routed at the
  current layout (`start` is captured before `try_route`) and processes them in topological order,
  so the ASAP accumulation across a batch is right.
- **`only_interactions`** does not panic on empty `indices` (the loop is simply empty).
- **Behaviour-preserving refactor.** `weight * (*score + ΔF)` equals the old `weight * ΔF` when
  the credit is zero, so `SetScaling::Size` and lookahead arithmetic are unchanged apart from the
  credit itself.
- **No double counting of the wait term** across layers or decay (it is added once, after decay).
- `cargo fmt --check` is clean. Clippy is not (finding 6b).

## Verification

Built from `git archive HEAD` in a scratch directory with
`QISKIT_BUILD_PROFILE=release pip install -e . ddt` (Python 3.11.8, macOS arm64). Commands were run
from the scratch source root.

| Check | Result |
| --- | --- |
| `test_sabre_swap`, `test_sabre_layout`, `test_sabre_pre_layout`, `test_transpile_layout`, `test_preset_passmanagers`, `compiler.test_transpiler` | 829 run, **1 failure** (finding 6), 3 skipped |
| `cargo fmt --all -- --check` | clean |
| `cargo clippy -p qiskit-transpiler --all-targets -- -D warnings` | **3 errors**, all in `route.rs` (finding 6b) |
| Level 0/1 CX counts, `test_lookahead_mode` circuit (finding 1) | two-swap route 12 / 10 CX vs. one-swap route 9 / 9 CX |
| Parameterized `rz(θ)` after `cx(1, 2)` (finding 2) | 10 CX after resynthesis vs. 8 bound, 9 for the one-swap route |

The experiment scripts are small (`SabreSwap` with `trials=1`, seeds 0 to 2 or 0 to 4, then either
`transpile(..., basis_gates=["cx", "u"], initial_layout=[0, 1, 2, 3, 4])` or
`PassManager([ConsolidateBlocks(basis_gates=["cx", "u"]), UnitarySynthesis(["cx", "u"])])`) and
are reproduced in full by the tables above.

## Suggested order of work

1. Finding 2 (`block_gates` predicate) and finding 7 (`Some(2)` bucket): small, local, and they
   directly affect the benchmark the weight was tuned on, so re-tune `READY_WEIGHT` afterwards.
2. Finding 5 (expose both terms through `Heuristic`) together with finding 1 (enable the credit
   from the presets only at levels 2 and 3), plus docstrings and the release note.
3. Finding 3 (selection criterion) and finding 4 (wait-term scaling and cap).
4. Findings 6 and 6b (fix or loosen the level-3 pin, make clippy pass) before merge; findings 8
   to 11 as follow-ups.
