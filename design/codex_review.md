**Review of `90ccaac29b2a547592e870797265007a128a02eb` — Include gate fusion and wire wait time in Sabre swap scoring**

Reviewed on 2026-10-10 against parent `1298f960b7e814cfcb6aa9338b548ec5c6d2b355`, including the Rust implementation, changed tests, and existing design notes.

Five findings are listed below. **None is critical:** I found no evidence of incorrect quantum semantics, a crash, or broken routing legality. The issues affect routing quality, consistency of the new heuristic, and test reliability. All are **P2 / medium priority**; the compressed-node timing issue is a limitation of the new model rather than a demonstrated regression against the parent.

**1. [P2] Fusion credit crosses operations that block actual resynthesis**

Location: [dag.rs:94–110](/Users/ophir/PycharmProjects/qiskit-evolved/crates/transpiler/src/passes/sabre/dag.rs:94).

`block_gates` excludes a few operation variants, then treats every remaining one-qubit operation as `Some(0)` and every remaining two-qubit operation as a fusible gate. This includes opaque/nonunitary Python instructions and operations with unbound parameters. The function receives only `PackedOperation`, so it cannot inspect the instruction's parameter values.

That does not match the downstream block collector: [DAGCircuit::collect_2q_runs](/Users/ophir/PycharmProjects/qiskit-evolved/crates/circuit/src/dag_circuit.rs:5933) rejects general instructions and parameterized gates. Consequently, the router can purchase an extra SWAP using a discount that the optimizer cannot realize.

Reproduction, using a five-qubit line and `SabreSwap(..., "lookahead", seed=0, trials=1)`:

```python
qc = QuantumCircuit(5)
qc.cx(0, 1)
qc.cx(2, 3)
qc.h(0)
qc.cx(1, 2)
qc.rx(Parameter("theta"), 1)  # Also reproduces with an opaque 1q Instruction.
qc.cx(1, 3)
qc.cx(2, 3)
qc.cx(1, 3)
```

After `ConsolidateBlocks(basis_gates=["cx", "u"])` and `UnitarySynthesis(["cx", "u"])`:

| Version | Inserted SWAP pairs, in order | CX count after synthesis |
| --- | --- | --- |
| Parent | `(2, 3)` | 9 |
| Reviewed commit | `(1, 2)`, `(2, 3)` | 10 |

The same result occurs when replacing `rx(theta, 1)` with `qc.append(Instruction("opaque", 1, 0, []), [1])`. Instrumentation confirms that the preceding `(1, 2)` block remains eligible for the discount.

**Suggested fix:** classify the full instruction, including parameters. Share the block collector's eligibility rules where possible, and conservatively close the block for instructions or custom operations whose unitary synthesis eligibility is unknown. Add tests for an opaque one-qubit instruction, a nonunitary two-qubit instruction, and unbound one-/two-qubit gates. Check the resulting native gate count, not only which SWAP was chosen.

**2. [P2] Layout-search graphs retain fusion credit across barriers and control flow**

Locations: [route.rs:745–753](/Users/ophir/PycharmProjects/qiskit-evolved/crates/transpiler/src/passes/sabre/route.rs:745), [dag.rs:270–273](/Users/ophir/PycharmProjects/qiskit-evolved/crates/transpiler/src/passes/sabre/dag.rs:270).

The new `advance_routed` clears fusion state for synchronization and control-flow nodes by iterating over `node.indices`. However, `only_interactions()` deliberately empties those indices. SABRE's forward/backward layout iterations use this simplified graph, so their block-clearing loop does nothing. Copying `node.block = None` does not help: the synchronization branch never reads it.

I traced `SabreLayout` on a four-qubit line with `cx(0, 1); barrier(); cx(0, 3)`, using `seed=0`, `swap_trials=1`, `layout_trials=1`, and `max_iterations=1`. An exploratory pass visits `Synchronize indices=[] block=None` and then still gives the preceding physical pair its `-2/3` fusion credit. The full graph contains the barrier's index and clears that state correctly.

This makes layout search optimize against fusion opportunities that do not exist during final routing. The existing guide mentions this behavior, but it is a representation inconsistency, not a difference in physical gate cost.

**Suggested fix:** store the affected virtual qubits/block-boundary metadata directly on `SabreNode`, independently of reconstruction indices, and preserve it through `only_interactions()` and reversal. Use that metadata to invalidate fusion state. Add a Rust-level test comparing state/candidate credits in the original and simplified graphs for the same layout; include a barrier, a measurement with classical dependencies, and control flow.

**3. [P2] Barriers do not synchronize wire-ready times, creating false parallelism**

Location: [route.rs:745–753](/Users/ophir/PycharmProjects/qiskit-evolved/crates/transpiler/src/passes/sabre/route.rs:745).

The synchronization branch clears `last_block` but never updates `ready`. After a barrier, wires that were idle before it retain time zero even though operations on those wires must wait for the barrier's other predecessors. The new score can therefore pay for apparent parallelism that the emitted circuit cannot exploit.

Use the coupling edges `(0,1), (1,2), (2,3), (3,4), (1,5)`, made symmetric, and this heuristic:

```python
h = (Heuristic(attempt_limit=60)
     .with_basic(1.0, SetScaling.Constant)
     .with_lookahead([1e-4], SetScaling.Constant))

qc = QuantumCircuit(6)
for _ in range(10):
    qc.cx(0, 1)
    qc.cx(5, 1)
qc.barrier()
qc.cx(1, 3)
qc.cx(3, 4)
```

With `seed=0, trials=1`, the parent inserts one SWAP, `(1,2)`. This commit inserts `(2,3)` followed by `(3,4)`. Translating SWAPs to CX with `BasisTranslator`, while retaining the barrier, gives **CX depth 25 in the parent versus 27 in this commit**.

The trace shows `ready[1]=20` and `ready[2]=ready[3]=0` after the global barrier. Both candidate SWAPs actually start no earlier than layer 20. Without the false wait difference, lookahead correctly prefers `(1,2)`.

**Suggested fix:** for a barrier, set all participating wire-ready times to their maximum without adding a gate layer. Preserve the touched-wire metadata in the simplified graph too. Extend the model separately for other synchronization dependencies. Add this barrier variant of `test_wire_ready_time_is_priced_against_lookahead` and assert the native depth/SWAP count.

**4. [P2] Compressed interaction nodes can hide arbitrarily long nonfusible gate runs**

Location: [route.rs:732–736](/Users/ophir/PycharmProjects/qiskit-evolved/crates/transpiler/src/passes/sabre/route.rs:732).

`advance_routed` adds exactly one layer per `TwoQ` SABRE node. Such a node can contain many original two-qubit operations, including intervening barriers that prevent their fusion. Counting the node as one layer is therefore not just an approximation of gate duration: it can undercount an arbitrarily long sequence of mandatory layers.

Using the coupling map and heuristic from finding 3, replace its prefix with:

```python
qc = QuantumCircuit(6)
for _ in range(20):
    qc.cx(0, 1)
    qc.barrier(0, 1)
qc.cx(1, 3)
qc.cx(3, 4)
```

All 40 prefix operations compress into one SABRE node. The trace reports `ready[0]=ready[1]=1`, although there are 20 CX layers that cannot fuse across their barriers. The wait penalty becomes `2.2e-5` instead of `4.4e-4`, reversing its intended comparison with the `1e-4` lookahead preference.

The router chooses `(1,2)` and yields CX depth 25 after basis translation. A legal alternative, `(2,3); cx(1,2); (3,4); cx(2,3)` after the same prefix, has depth 22 because its SWAPs can overlap the prefix. The parent also chooses the first route: this finding concerns fidelity of the newly introduced timing signal, not a parent-to-commit regression. The existing design notes acknowledge node compression, but do not bound this error.

**Suggested fix:** precompute a separate depth/scheduling summary during DAG compression and retain it through layout search. Count nonfusible two-qubit segments and their synchronization constraints; do not reuse the fusion count capped at three as a duration. Add a test with repeated same-pair gates separated by barriers. The added idle-wire test only checks the sign of the timing difference and therefore misses this magnitude error.

**5. [P2] The updated level-3 Tokyo-layout expectation fails**

Location: [test_preset_passmanagers.py:1070](/Users/ophir/PycharmProjects/qiskit-evolved/test/python/transpiler/test_preset_passmanagers.py:1070).

The newly pinned level-3 layout is not produced in the tested environment:

```text
test_layout_tokyo_fully_connected_cx_4_3
Expected: [5, 11, 6, 10, 0]
Actual:   [5, 6, 11, 10, 0]
```

The parent version of this test passes in the same environment. This independently reproduces the discrepancy already recorded in `design/SABRE_CHANGE_EXPLAINED.md`. It is a test-suite failure, not evidence that the actual layout is invalid.

**Suggested fix:** establish the expected behavior on supported build environments before updating the fixture. If the exact end-to-end permutation depends on numerical synthesis/platform behavior, test layout correctness/composition and repeatability with fixed trial counts, or isolate the deterministic pass whose permutation is intended to be pinned. Do not simply remove the assertion. Re-run all parameterized levels after changing it.

**Validation and limits**

Built the reviewed commit and its parent from temporary source copies, using Rust 1.89 on Darwin arm64, Python 3.11.11, NumPy 2.4.6, SciPy 1.17.1, and rustworkx 0.18.1. The reviewed build had conditional diagnostic logging added in the temporary copy; logging did not change the scoring calculations. Production source files were not edited.

Ran the following against the reviewed build:

```sh
python -m unittest \
  test.python.transpiler.test_sabre_swap \
  test.python.transpiler.test_sabre_layout \
  test.python.transpiler.test_preset_passmanagers.TestFinalLayouts.test_layout_tokyo_fully_connected_cx_4_3
```

Result: **170 tests run; 168 passed, 1 skipped, 1 failed** (finding 5). This includes the newly added fusion and wait-time tests. The targeted parent Tokyo-layout test passed. Additional small circuits reproduced the state/quality issues described above; CX-depth comparisons use `BasisTranslator(SessionEquivalenceLibrary, ["cx", "u"])` and count only CX instructions in the depth filter, so two-qubit barriers are not counted as gates.

The full repository suite and the performance panels in `RATIONALE.md` were not rerun. The report makes no claim about aggregate performance gains or cross-platform prevalence of the pinned-layout failure.
