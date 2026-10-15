# Benchpress plan: SABRE fusion and wire-wait scoring versus baseline

Prepared on 2026-10-14. This document specifies the cluster comparison. The [harness is implemented](../tools/benchpress/README.md); local adapter checks have run, but Linux builds of the two pinned revisions and the LSF campaign have not run.

The exact Qiskit hashes below describe the original experiment. The launcher also supports branch comparisons, defaulting to `main` versus `HEAD`; use `--candidate-ref sabre-gate-fusion-aware-scoring` to select another branch. It resolves refs to fixed commits at submission without requiring a parent relationship. All profiles default to L2 and L3. See the [current submission guide](../tools/benchpress/README.md#submit) for branch-based commands.

**Run one quality and compilation-runtime comparison at optimization levels 2 and 3.** The `quality` profile uses the fixed 50-entry circuit/target panel with **three transpiler seeds (`0,1,2`) and one transpilation per revision/level/seed**. Measure the wall-clock runtime of that single transpilation callable as well as its circuit quality. Each LSF array element runs both revisions at both levels sequentially on the same host. Run **16 array elements concurrently** initially: **150 array elements / 600 invocations**. Build and correctness validation are followed directly by this array and its report. Job timeouts and a three-hour campaign deadline remain execution controls, distinct from the measured callable runtime. The full corpus is optional.

The detailed implementation specification, CLI arguments, LSF submission sequence, retry behavior, and shared-filesystem output layout are in [benchpress_lsf_implementation_plan.md](benchpress_lsf_implementation_plan.md). That document defines the cluster workflow; the direct build/smoke commands below are manual reference steps, not instructions to build on the laptop or run benchmarks on an LSF login node.

## 1. Comparison and questions

Compare these exact commits from this repository:

| Role | Commit | Description |
| --- | --- | --- |
| Baseline | `1298f960b7e814cfcb6aa9338b548ec5c6d2b355` | Parent of the SABRE change; currently also local `main` and `origin/main`. |
| Candidate | `90ccaac29b2a547592e870797265007a128a02eb` | Algorithm commit: “Include gate fusion and wire wait time in Sabre swap scoring”. |
| Planning branch tip at inspection | `ff29e0f5650f4ae3f0bf4ddb86c97d1bd5a6a12c` | Current branch `sabre-choose-best-swap-heuristics-benchpress`; adds the plan and presentation above the algorithm commit. |

The baseline is **the parent of the algorithm commit**, not the parent of the current planning branch tip. At inspection, current `HEAD^` is the candidate and `HEAD^^` is the baseline; future planning/runner commits will move those offsets again. Always use the explicit Qiskit hashes above. Separately record the harness commit containing the implementation used to submit/run the jobs. Stay on the existing `sabre-choose-best-swap-heuristics-benchpress` branch for this work; do not reset or repurpose its history. The algorithm changes are in `crates/transpiler/src/passes/sabre/dag.rs` and `route.rs`; the other five changed files update tests. Both Qiskit revisions report `2.6.0.dev0`, so the package version alone cannot identify them.

Answer three questions:

1. Does the candidate reduce final native two-qubit gate count and two-qubit depth after transpilation and optimization, separately at levels 2 and 3?
2. Are these quality changes consistent across circuits, connectivity, and transpiler seeds, without new correctness failures?
3. How does the candidate change compilation runtime in seconds and percent, and which quality improvements coincide with slower or faster transpilation?

Raw SWAP count is a diagnostic, not the primary quality measure. This change explicitly anticipates fusion during later synthesis; a route with more SWAPs can produce fewer native gates. The wire-ready score estimates depth rather than calibrated hardware execution time. Do not report a hardware speedup from that score or from circuit depth alone.

## 2. Verified Benchpress behavior

Use [Qiskit/benchpress](https://github.com/Qiskit/benchpress) at inspected commit `b695f30e83a32bac05b9b4d8e98d37ba9aae5236` for both revisions.

| Finding | Consequence for this experiment |
| --- | --- |
| Benchpress runs directly with pytest; its Qiskit tests live in `benchpress/qiskit_gym`. | Run from the Benchpress checkout with the selected environment's Python. |
| Relevant suites are `device_transpile` and `abstract_transpile`. | Make these the comparison; construction/manipulation suites are optional controls. |
| Upstream defaults are optimization level 2, `fake_torino`, and abstract topologies `all-to-all`, `square`, `heavy-hex`, `linear`. | Run levels **2 and 3**, using separate immutable config files; retain the default CZ basis. Device tests use their backend's native basis. |
| Transpilation tests call `pm.run(circuit)` through the benchmark fixture. | Use `--benchmark-disable` with timeout preflight disabled so the callable executes once without calibration or repeated rounds; measure that call in the adapter using `time.perf_counter()`. |
| Quality metrics are stored in `benchmark.extra_info`: `output_gate_count_2q`, `output_depth_2q`, `output_circuit_operations`, and `output_num_qubits`. | Export these to `quality.json` through the adapter; disabled benchmark timing does not produce the usual statistics records. |
| Those count/depth metrics filter on the backend's single native entangling gate, such as CZ. | Record the gate name; do not assume the metric always counts CX or recursively covers control-flow blocks. |
| The relevant pass-manager calls omit `seed_transpiler`. `FlexibleBackend` also omits a seed when constructing `GenericBackendV2`. | Apply the same small reproducibility patch to the shared Benchpress checkout before either run. |
| The Qiskit requirements use a custom `pytest-benchmark` Git fork. | Install that fork at a fixed SHA, not the stock package or a moving branch. |
| Validation checks supported gates and connectivity. | Keep it enabled, and supplement it with small semantic checks; a passing benchmark is not an equivalence proof. |

These observations come from the pinned [device tests](https://github.com/Qiskit/benchpress/tree/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/device_transpile), [abstract tests](https://github.com/Qiskit/benchpress/tree/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/abstract_transpile), [configuration](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/default.conf), [metrics implementation](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/utils/io.py), and [validator](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/utils/validation.py).

## 3. Isolated source trees and release builds

Use two pinned source snapshots or detached worktrees, two runtime environments, and separate wheel and Rust build directories. Leave the development checkout's existing `.gitignore` changes intact. Build Linux wheels/environments on the cluster's compute architecture and shared filesystem; the local macOS environment cannot be copied to Linux workers. The commands below describe manual build mechanics; the LSF build job will carry them out with cluster paths.

Prerequisites: the same Python 3.12 interpreter for both environments, a C toolchain, and the Rust toolchain selected by `rust-toolchain.toml` (`1.89`). Qiskit declares Python >=3.10 and Rust >=1.89; Python 3.12 is the proposed common choice, subject to the dependency smoke test.

```bash
REPO=/shared/PROJECT/USER/qiskit-evolved
RUN_ROOT=/shared/PROJECT/USER/benchpress-runs/2026-10-14-quality-o2-o3
BASELINE=1298f960b7e814cfcb6aa9338b548ec5c6d2b355
CANDIDATE=90ccaac29b2a547592e870797265007a128a02eb
BENCHPRESS=b695f30e83a32bac05b9b4d8e98d37ba9aae5236
BENCHMARK_PLUGIN=e33f5934cb462e719b13b8d352ced107f575b4f7

mkdir -p "$RUN_ROOT/src" "$RUN_ROOT/wheels" "$RUN_ROOT/results"
test "$(git -C "$REPO" rev-parse "$CANDIDATE^")" = "$BASELINE"
git -C "$REPO" worktree add --detach "$RUN_ROOT/src/baseline" "$BASELINE"
git -C "$REPO" worktree add --detach "$RUN_ROOT/src/candidate" "$CANDIDATE"
git clone https://github.com/Qiskit/benchpress.git "$RUN_ROOT/benchpress"
git -C "$RUN_ROOT/benchpress" checkout --detach "$BENCHPRESS"

python3.12 -m venv "$RUN_ROOT/build-env"
"$RUN_ROOT/build-env/bin/python" -m pip install \
  'setuptools>=77.0' 'setuptools-rust>=1.13.0' wheel
"$RUN_ROOT/build-env/bin/python" -m pip freeze --all > "$RUN_ROOT/build-requirements.lock"

unset RUST_DEBUG RUSTFLAGS CARGO_ENCODED_RUSTFLAGS
unset QISKIT_NO_CACHE_GATES QISKIT_BUILD_WITH_MIMALLOC
export QISKIT_BUILD_PROFILE=release
for revision in baseline candidate; do
  mkdir -p "$RUN_ROOT/wheels/$revision"
  CARGO_TARGET_DIR="$RUN_ROOT/target/$revision" \
    "$RUN_ROOT/build-env/bin/python" -m pip wheel \
    --no-build-isolation --no-deps \
    --wheel-dir "$RUN_ROOT/wheels/$revision" "$RUN_ROOT/src/$revision" \
    > "$RUN_ROOT/results/build-$revision.log" 2>&1 || exit 1
done
```

Inspect both logs for release builds. Archive compiler versions, build flags, `Cargo.lock` hashes, and wheel SHA-256 hashes. Do not use an existing editable installation: it risks stale Rust extensions or imports from the development checkout. Both wheels have the same version and possibly the same filename, so keep their directories distinct.

Resolve shared runtime dependencies once against the baseline wheel, then install the identical resolved dependencies for the candidate. The following replaces the two moving Qiskit-specific requirements with a local wheel and a pinned plugin; the remaining direct requirement is `qiskit-ibm-runtime`.

```bash
python3.12 -m venv "$RUN_ROOT/env-baseline"
python3.12 -m venv "$RUN_ROOT/env-candidate"
BASE_PY="$RUN_ROOT/env-baseline/bin/python"
CAND_PY="$RUN_ROOT/env-candidate/bin/python"

"$BASE_PY" -m pip install \
  "$RUN_ROOT"/wheels/baseline/*.whl \
  -r "$RUN_ROOT/benchpress/requirements.txt" \
  qiskit-ibm-runtime \
  "pytest-benchmark @ git+https://github.com/1ucian0/pytest-benchmark.git@$BENCHMARK_PLUGIN"
"$BASE_PY" -m pip freeze > "$RUN_ROOT/baseline.freeze.txt"
"$BASE_PY" - "$RUN_ROOT" <<'PY'
import pathlib, re, sys
root = pathlib.Path(sys.argv[1])
lines = (root / 'baseline.freeze.txt').read_text().splitlines()
shared = [line for line in lines if not re.match(r'^qiskit(?:==|\s*@\s*)', line, re.I)]
(root / 'shared-requirements.lock').write_text('\n'.join(shared) + '\n')
PY
"$CAND_PY" -m pip install --no-deps \
  -r "$RUN_ROOT/shared-requirements.lock" "$RUN_ROOT"/wheels/candidate/*.whl
"$BASE_PY" -m pip check
"$CAND_PY" -m pip check
```

Stop on a build, resolution, or `pip check` failure. If a compatibility adjustment is needed, make it common to both environments and regenerate the lock. Archive a wheelhouse for long-term reproducibility if practical. The upstream [Qiskit requirements](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/requirements-qiskit.txt) explain the fork dependency.

Before benchmarking, run an import check from the Benchpress directory in each environment. Record `sys.executable`, `qiskit.__version__`, `qiskit.__file__`, `qiskit._accelerate.__file__`, distribution `direct_url.json`, and a hash of the extension. Verify each path belongs to the intended environment and each installation points to its own wheel. Compare dependency inventories after excluding Qiskit. Do not trust the version string or pytest's automatically inferred Git metadata, which may describe Benchpress rather than the Qiskit build.

## 4. Reproducibility patch and run configuration

Prepare one patch against the Benchpress checkout and save it as `results/benchpress-reproducibility.patch`. Apply it once and reuse the resulting checkout for both environments. The patch is generated by `tools/benchpress/adapter.py` and archived for each campaign.

1. Add `seed_transpiler=int(os.environ["BENCHPRESS_TRANSPILE_SEED"])` to every `generate_preset_pass_manager` call in the three device and two abstract transpilation files. Add the necessary imports. Preserve default layout/routing choices; set the optimization level through the selected immutable config file.
2. In `benchpress/utilities/backends/flexible_backend.py`, pass `seed=int(os.environ["BENCHPRESS_BACKEND_SEED"])` to the `GenericBackendV2` superclass constructor. Keep `noise_info` unchanged. A NumPy global seed alone will not control its independently constructed `default_rng`.
3. Add seed and configuration identifiers to the output metadata. Preserve the existing metrics and validation. Make missing seed variables fail clearly rather than silently restoring randomness.
4. Sort collected node IDs in a pytest collection hook, then save the exact ordered selection. Reuse it for both revisions. This avoids filesystem-order differences in Feynman/QASMBench discovery.
5. Add a `BENCHPRESS_CONFIG` environment override to `BenchpressConfig`'s filename selection, falling back to its upstream default only outside the controlled run. Each child uses an absolute path to an archived `opt-2.conf` or `opt-3.conf` before importing Benchpress. Each child also receives its own working directory, pytest cache, temporary directory, and output paths. Never edit a shared `default.conf` or share `skipfile.txt` between array elements.
6. Run pytest with `--benchmark-disable --timeout-skip-list=0`; reject upstream benchmark repetition/profiling options in controlled jobs. The pinned fixture's disabled path calls the transpilation callable once. Disabling upstream timing alone is insufficient if a timeout preflight is enabled, because that would call it again. Wrap the actual benchmark callable with `time.perf_counter()` and export its elapsed seconds as `runtime_s`, with `runtime_method="perf_counter"` and `runtime_scope="benchmark_callable"`. Exclude circuit loading, pass-manager construction, pytest startup, output-metric extraction, and validation from the interval. Add an export hook that writes these timing fields alongside identity, quality fields, and final validation status to `quality.json`, including failure/skip outcomes. Do not depend on upstream benchmark statistics or benchmark JSON.
7. In quality mode, continue to bypass the QASM loader's `perf_counter()` calls and `qasm_load_time` field. Pytest/LSF may still include incidental job durations in diagnostic logs; the collector never substitutes these durations for callable runtime. Require finite positive timing samples for successful runtime comparisons and preserve missing/invalid timing separately from quality validity.

The single-call behavior is verified in the pinned [pytest-benchmark fixture](https://github.com/1ucian0/pytest-benchmark/blob/e33f5934cb462e719b13b8d352ced107f575b4f7/src/pytest_benchmark/fixture.py). Test the exporter independently: disabling the benchmark fixture must preserve gate/depth metrics and native validation while the adapter times exactly one callable execution.

Use these settings before importing Qiskit in every benchmark process:

```bash
unset PYTHONPATH PYTEST_ADDOPTS
unset QISKIT_SABRE_ALL_THREADS QISKIT_IN_PARALLEL QISKIT_FORCE_THREADS
export QISKIT_IGNORE_USER_SETTINGS=TRUE
export QISKIT_PARALLEL=FALSE
export QISKIT_NUM_PROCS=1
export RAYON_NUM_THREADS=1
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
export VECLIB_MAXIMUM_THREADS=1 NUMEXPR_NUM_THREADS=1
export PYTHONHASHSEED=0
export BENCHPRESS_BACKEND_SEED=424242
export BENCHPRESS_TRANSPILE_SEED=0
export BENCHPRESS_QUALITY_ONLY=1
```

This defines one CPU thread per benchmark process. Parallelism comes from independent LSF array elements. `QISKIT_NUM_PROCS=1` does **not** force the preset pipeline to use one SABRE trial: at this revision, levels 2 and 3 use 20 trials, with default SABRE layout iteration limits of 2 and 4 respectively. Record these differences and preserve them for both revisions. Direct `SabreSwap`/`SabreLayout` diagnostics must set trial counts explicitly. Multithreading within a benchmark remains a separate experiment.

Create `opt-2.conf` and `opt-3.conf` from the pinned default, changing only `[qiskit].optimization_level`. Hash and archive both. Upstream Benchpress has no assumed general `--config` CLI flag; the planned `BENCHPRESS_CONFIG` adapter supplies the explicit path. Compare candidate versus baseline within each level, and report the quality effects of level 3 separately. A baseline-level-2 versus candidate-level-3 comparison would mix two interventions.

Use the packaged fake Torino backend without refreshing it from a service. Save hashes of its packaged configuration/properties and the runtime package version. For abstract targets, save a canonical fingerprint of qubit count, sorted edges, operation names, gate errors, and durations. Confirm target fingerprints match between revisions. These runs need no hardware jobs or IBM account credentials.

## 5. Circuits, seeds, and workload

### 5.1 Circuit inventory

A **panel entry** is one circuit on one target. A **case** adds one optimization level and thread configuration. The 50-entry panel becomes **100 cases** when levels 2 and 3 are enabled. The inventory table below counts entries (equivalently, cases at one level); seeds do not create new cases.

The following inventory is the **available corpus for selection**, not the default execution scope. The quality profile selects the fixed 50 entries from it; the optional full profile attempts the eligible corpus. Counts were checked against the pinned Benchpress source tree and HamLib input data; they are source inventory counts, not a claim that pytest has collected or successfully executed them yet.

| Suite / input | Circuits | Targets per circuit | Entries before exclusions | What it exercises |
| --- | ---: | ---: | ---: | --- |
| Device Summit: `device_transpile/test_summit.py` | 9 | Fake Torino | 9 | Large structured circuits, random two-qubit interactions, and simplification controls. |
| Device Feynman: all `.qasm` files directly under `benchpress/qasm/feynman/` | 53 | Fake Torino | 53 | Reversible arithmetic, multi-controlled gates, QFT, error correction, and other algorithmic circuits. |
| Device HamLib: `benchpress/hamiltonian/hamlib/100_representative.json` | 100 | Fake Torino | 100 | Hamiltonian evolution circuits from chemistry, condensed matter, and optimization problems. |
| Abstract QASMBench small: `benchpress/qasm/qasmbench-small/` | 42 | Four topologies | 168 | Small algorithms, entanglement, variational circuits, and correctness/control-flow examples. |
| Abstract QASMBench medium: `benchpress/qasm/qasmbench-medium/` | 23 | Four topologies | 92 | Arithmetic, QFT, quantum memory, classifiers, and state preparation. |
| Abstract QASMBench large: `benchpress/qasm/qasmbench-large/` | 58 | Four topologies | 232 | Larger instances of the same families plus Quantum Volume, QUGAN, and UCCSD. |
| Abstract HamLib: the same 100 representative Hamiltonians | 100 | Four topologies | 400 | How Hamiltonian circuits respond to different connectivity constraints. |
| **Total** | | | **1,054** | **162 device entries + 892 abstract entries.** |

The four abstract topologies are `linear`, `square`, `heavy-hex`, and `all-to-all`; each uses the default `['id', 'sx', 'x', 'rz', 'cz']` basis. Run both optimization levels 2 and 3 with the single-thread settings from section 4. Construction/manipulation and within-process multithreading are outside this experiment. The 1,054-entry full inventory expands to 2,108 level-specific cases.

The fixed 50-entry panel includes these nine Summit tests:

| Circuit / test suffix | Input details | Why include it |
| --- | --- | --- |
| `test_QFT_100_transpile` | `qft_N100.qasm`, 100 qubits | Dense interaction structure requiring substantial routing. |
| `test_QV_100_transpile` | `QuantumVolume(100, 100, seed=12345)` | Random two-qubit unitary layers; the input-generation seed stays fixed. |
| `test_circSU2_89_transpile` | 89-qubit EfficientSU2, 3 repetitions, circular entanglement | Structured variational routing; also used for smoke testing. |
| `test_circSU2_100_transpile` | 100-qubit EfficientSU2, 3 repetitions, circular entanglement | A second size of the same structure. |
| `test_BV_100_transpile` | 100-qubit Bernstein–Vazirani, all-ones secret | Star-like interactions. |
| `test_square_heisenberg_100_transpile` | `square_heisenberg_N100.qasm` | A lattice interaction pattern mapped to the device. |
| `test_QAOA_100_transpile` | `qaoa_barabasi_albert_N100_3reps.qasm` | Graph interactions over three QAOA repetitions. |
| `test_BVlike_simplification_transpile` | 100-qubit BV-like circuit | Optimization control expected to simplify strongly. |
| `test_clifford_100_transpile` | `clifford_100_12345.qasm` | Clifford synthesis and routing. |

The dataset suites provide the following inputs. The quality run uses the selections in section 5.2; only the optional full profile attempts every eligible input:

- **Feynman:** includes `adder`, `adder_8`, `bigadder`, `gf2^4_mult` through larger finite-field multipliers, `barenco_tof_*`, `tof_*`, `qcla_*`, `grover_5`, `qft`, `qft_4`, `qec`, and `teleport`. Each `.qasm` filename defines an input.
- **QASMBench small:** examples include `adder_n4`, `adder_n10`, `qft_n4`, `qaoa_n3`, `qaoa_n6`, `hhl_n7`, `ising_n10`, and `vqe_uccsd_n4/n6/n8`.
- **QASMBench medium:** examples include `bigadder_n18`, `bv_n14`, `qft_n18`, `qram_n20`, `ising_n26`, `multiplier_n15`, `swap_test_n25`, and `wstate_n27`.
- **QASMBench large:** includes QV inputs in `QV_n32/32.qasm` and `QV_n100/100.qasm`, `adder_n28/n64/n118/n433`, `qft_n29/n63/n160/n320`, `ising_n34/n42/n66/n98/n420`, and larger BV, GHZ, classifier, QUGAN, and W-state instances. Expand the slash-separated names into separate inputs. Follow the loader's rule excluding filenames containing `transpiled`.
- **HamLib:** 35 chemistry, 35 condensed-matter, 15 binary-optimization, and 15 discrete-optimization records. These include molecular electronic/vibrational problems, transverse-field Ising and Heisenberg models, Bose/Fermi Hubbard models, graph-coloring, SAT, and travelling-salesperson instances. The 100 records span **2–930 input qubits**. If the resolved Fake Torino has 133 qubits, 81 HamLib records fit and 19 are expected capacity skips in the device suite. Abstract targets are sized to the circuit and have no such 133-qubit limit, although large cases may exceed the resource budget.

The input rules are defined by the pinned [Feynman test](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/device_transpile/test_feynman.py), [QASMBench parameterization](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/workouts/abstract_transpile/qasmbench.py), and [HamLib data](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/hamiltonian/hamlib/100_representative.json). The exact executed membership will be saved in `results/cases.jsonl`, including full pytest node IDs, source paths or Hamiltonian record identities, input hashes, topology, and exclusion reasons. Reconcile the 1,054 expected entries with collection at each level before running; investigate discrepancies instead of silently changing the denominator.

### 5.2 Fixed 50-entry panel

Select the panel before examining candidate improvements. The proposed allocation is:

| Family | Selected inputs | Targets | Entries |
| --- | --- | --- | ---: |
| Device Summit | The nine tests listed above | Fake Torino | 9 |
| Device Feynman | `adder_8`, `barenco_tof_5`, `gf2^8_mult`, `qft_4`, `rc_adder_6` | Fake Torino | 5 |
| Device HamLib | One record from each of the four categories, selected by the rule below | Fake Torino | 4 |
| Abstract QASMBench small | `adder_n10`, `qaoa_n6` | All four abstract topologies | 8 |
| Abstract QASMBench medium | `qft_n18`, `ising_n26` | All four abstract topologies | 8 |
| Abstract QASMBench large | `qft_n29`, `swap_test_n41` | All four abstract topologies | 8 |
| Abstract HamLib | The chemistry and condensed-matter records selected for device HamLib | All four abstract topologies | 8 |
| **Total** | | | **50** |

For each HamLib category (`chemistry`, `condensedmatter`, `binaryoptimization`, `discreteoptimization`), filter to 8–32 input qubits and device capacity. Sort by `(ham_qubits, ham_problem, ham_instance)` and choose the record at zero-based index `len(records) // 2`. Record its complete identity and input hash; do not rely on `ham_instance` alone, which need not identify the problem. If a category has no eligible record, record the gap and choose the smallest device-compatible record in that category. This rule limits initial cost while retaining all four categories, and does not use benchmark outcomes.

At the pinned dataset revision, that rule selects:

| Category | `ham_problem` | `ham_instance` | Qubits |
| --- | --- | --- | ---: |
| Chemistry | `Li2` | `/ham_JW14,` | 14 |
| Condensed matter | `BH_D-1_d-4` | `/bh_graph-1D-grid-pbc-qubitnodes_Lx-14_U-10_enc-stdbinary_d-4,` | 28 |
| Binary optimization | `graph-complete_bipart` | `/complbipart-n-10_a-5_b-5,` | 10 |
| Discrete optimization | `TSP_Ncity-5` | `/tsp_prob-ts225_Ncity-5_enc-unary,` | 25 |

Freeze all 50 entries before execution, resolving exact pytest node IDs and input/configuration hashes. Run the same panel at both optimization levels and both revisions. Preserve unsupported inputs, failures, and resource-limit outcomes in coverage; do not shrink the panel based on observed execution speed or quality results.

### 5.3 Seeds, optimization levels, and LSF task counts

| Quantity | Main quality comparison | Full comparison (optional) |
| --- | --- | --- |
| Panel entries | **50** | Eligible full corpus |
| Optimization levels | **2 and 3** | 2 and 3 unless explicitly narrowed |
| Transpiler seeds | **`0, 1, 2`** | `0, 1, 2, 3, 4` |
| Processes per seed/revision/level | **1** | 1 |
| Transpilation calls per process | **1** | 1 |
| Invocations per panel entry | **12** | 20 |
| LSF array elements per panel entry | **3** | 5 |

One array element owns `(panel_entry, seed)` and executes baseline/candidate at both levels sequentially. Use a fixed archived order, such as `L2/baseline, L2/candidate, L3/baseline, L3/candidate`. Different elements run concurrently, initially capped at 16. Keep numerical-library/Rayon/Qiskit thread limits at one within each invocation.

For `N` panel entries, `L` levels, and `S` seeds, there are `NS` elements and `2NLS` invocations. The main run has **150 elements and 600 single-transpilation invocations**. An optional full run over all 1,054 inventory entries with five seeds has 5,270 elements / 21,080 invocations before eligibility exclusions; split it into site-supported array sizes while enforcing one campaign-wide concurrency cap.

Each revision pair uses the same transpiler seed, backend seed `424242`, and input. Quantum Volume's generation seed stays `12345`. The adapter records one elapsed-time sample; there are no process repetitions, calibration, warmups, or repeated benchmark rounds. The `quality`, `followup-quality`, and `full` profiles all use this execution mode: `full` expands corpus coverage and is not a repeated timing benchmark. Small correctness/reproducibility checks belong to build validation and do not add scientific rows to the main comparison.

Treat runtime comparisons as single-call observations under the recorded allocation. Seeds vary the transpilation, rather than repeating an identical computation solely to estimate timing noise. Fixed baseline-first execution can introduce order effects, and different elements can run on different CPU models. Same-host pairing and fixed threads reduce some variation but do not establish a calibrated speedup or its uncertainty. A dedicated repeated, order-balanced timing campaign would require a separate design.

### 5.4 Execution stages

A single submission starts the LSF manager, which performs:

1. **Build and correctness validation:** build both pinned Linux release wheels and environments; run each revision's own affected Qiskit tests (`test_sabre_swap`, `test_sabre_layout`, `test_preset_passmanagers`, `test_transpile_layout`, compiler `test_transpiler`). Verify wheel imports and collect Benchpress at both levels. Resolve and freeze the requested panel.
2. **Adapter smoke checks within validation:** SU2-89 and small `qaoa_n6`/linear on both revisions and levels: eight single-call invocations. Verify quality/runtime export, config selection, isolated directories, native validation, and a small semantic/reproducibility check. These are implementation checks, not a preliminary benchmark campaign.
3. **Quality array:** submit all 150 main elements directly after validation, with at most 16 running concurrently. Account for every scheduled invocation, including failures and unstarted tasks.
4. **Report:** after all elements settle, produce gate-count/depth and runtime comparisons per level, separate quality/runtime coverage, and level-3-versus-level-2 summaries.

Treat `all-to-all` and cases where SABRE does not run as separate controls. Use the queue's eligible hosts without added CPU-count, CPU-model, or load selectors. Record hardware and affinity for provenance and verify build/runtime platform compatibility. Each element retains its common host and fixed thread settings. No pytest-xdist or parallel children inside one element are required.

### 5.5 Job limits

Keep the **three-hour campaign deadline**, beginning when the first quality worker starts, as a limit on cluster work. It includes later queue waits and compute; build/validation, initial queue wait, and final reporting are separate. Enforce **300 seconds per child**, a **25-minute internal element deadline**, and a **30-minute LSF execution limit**. These limits control resource use; they do not measure compilation performance or predict completion time.

At the campaign deadline, cancel unfinished owned array jobs, confirm terminal states, and report partial coverage. Record unresolved scheduler queries or unconfirmed cancellation explicitly. The manager has a separate 24-hour execution limit. Sixteen workers reserve up to 16 physical cores and 128 GB at 8 GB each, across eligible hosts. Preserve Linux build artifacts for reuse. Report measured callable-runtime differences with the single-call limitations above; do not infer a campaign ETA from them.

### 5.6 Optional additional coverage

The main report stands on its own. A separate quality follow-up may use ten seeds on explicitly selected cases; ten entries at both levels require 100 elements / 400 invocations. Preserve selection reasons and first-attempt outcomes. Do not combine an outcome-selected follow-up with the main-panel average. The full corpus remains optional and also uses one call per revision/level/seed.

## 6. Timeouts, coverage, and correctness limits

Launch one selected node ID per pytest subprocess with `--benchmark-disable --timeout-skip-list=0`. Use an external process-group watchdog for the whole child; a stuck Rust call must not escape the 300-second limit. Avoid a plugin timeout preflight because it performs an extra transpilation. Keep the limits identical for both revisions and levels, and preserve an explicit timeout/missing record when no quality JSON or XML can be produced. An optional full session may use a 900-second child limit with correspondingly larger element limits, recorded before submission.

The pinned fork still consults `skipfile.txt` in the working directory with timing disabled. Start every child in a fresh private directory without that file. Any generated skipfile is a diagnostic artifact; it must never suppress another revision or seed. Retain failures and timeouts even if a later explicit retry succeeds.

Keep Benchpress's gate/connectivity validation. Add small semantic checks during build validation, accounting for initial/final layouts, ancillas, and global phase. Do not construct dense operators for 89/100-qubit circuits. Measurement/control-flow examples need appropriate dedicated checks.

For attribution, optional diagnostic replays can record whether `SabreLayout` or `SabreSwap` executes; a valid VF2 layout can avoid SABRE. Keep diagnostic results separate from the main quality rows and retain the original preset configuration in the campaign.

## 7. Analysis and decision rules

Join baseline/candidate by **profile/stage + node ID + optimization level + input/configuration/target fingerprint + seed + host/allocation + attempt**. Do not join by list position, package version, or short name. Calculate quality differences only when both records pass validation. Keep unpaired failures, skips, timeouts, cancellations, and missing artifacts in coverage.

| Field | Source / interpretation |
| --- | --- |
| Identity | Qiskit/harness/Benchpress/plugin hashes, wheel/extension hash, patch/lock/input/config/target hashes, node ID, seed |
| Quality | `quality.json` fields `extra_info.output_gate_count_2q` and `output_depth_2q` |
| Runtime | `quality.json` fields `runtime_s`, `runtime_method="perf_counter"`, `runtime_scope="benchmark_callable"`; one wall-clock sample per invocation |
| Context | Native two-qubit gate name, input/output width, operation histogram, topology, family, level, thread/trial settings |
| Outcome | Passed, skipped, failed, timeout, cancelled, not run, or missing artifact, with reason |

For positive baseline gate/depth metrics, report `100 * (candidate / baseline - 1)`; negative means an improvement. Also report absolute differences. For a zero baseline use absolute differences and a separate category; do not divide by zero or discard the row.

Summarize the three paired seeds per case before aggregating across cases. Report medians, geometric-mean ratios for positive metrics, win/tie/loss counts, and the largest regressions, separately by level, topology, and circuit family. Treat seeds as repeated observations of a circuit, not independent circuits. If reporting uncertainty, resample whole circuit groups and state the limited panel/seed coverage. Compare level 3 versus level 2 within each Qiskit revision on the common complete panel.

For each eligible runtime pair, calculate `runtime_change_s = candidate_runtime_s - baseline_runtime_s`, `runtime_ratio = candidate_runtime_s / baseline_runtime_s`, and `runtime_change_pct = 100 * (runtime_ratio - 1)`. Negative change and a ratio below one mean faster transpilation; positive change means slower. Both samples must be finite positive numbers with the same supported timer and measurement scope. Zero, negative, non-finite, boolean, or nonnumeric samples are invalid runtime measurements; do not divide by zero or interpret a failed invocation's partial duration as a successful sample.

Keep runtime completeness independent of quality completeness. Historical artifacts without timing remain valid quality results and have unavailable runtime; never reconstruct timing from timestamps, JUnit, or scheduler logs. A runtime-complete case has all configured seeds successfully quality-paired and runtime-paired. Report per-revision median seconds and medians of the **paired per-seed** seconds changes, ratios, and percentage changes. Do not replace paired differences with a difference of independent medians. Aggregate complete runtime cases separately per level, suite, and topology, with complete/missing counts, median changes, geometric-mean ratios, faster/tie/slower counts, and the largest slowdowns. Cross-level runtime comparisons require complete timing at both levels. Keep quality regressions and runtime regressions in separate tables, and show their tradeoffs in the report without dropping untimed quality results.

Review rules:

- Investigate every new correctness failure, crash, timeout, or missing result.
- List every gate-count/depth regression; highlight increases above 5% with absolute counts so small-circuit percentages remain interpretable.
- Report gate-count/depth/runtime tradeoffs explicitly. An overall quality claim must be supported by both coverage and paired summaries; a runtime comparison must state its own complete-case denominator.
- Keep all-to-all and no-routing controls visible separately. Describe runtime results as measured single-call compilation differences, and do not infer hardware execution speedups from circuit quality or compilation time.

## 8. Output format and artifacts

Use a fresh shared results directory for every campaign. All paths below describe future artifacts.

```text
<results-root>/
  build-requirements.lock
  shared-requirements.lock
  baseline.freeze.txt
  wheels/{baseline,candidate}/
  results/
    manifest.json
    profile.json
    panel.jsonl
    cases.jsonl
    benchpress-reproducibility.patch
    configs/{opt-2,opt-3}.conf
    build-{baseline,candidate}.log
    validation/
    raw/<stage>/<panel-id>/opt-<level>/seed-<s>/attempt-<a>/<revision>/
      run.json
      quality.json
      junit.xml
      stdout.log
      stderr.log
    tables/runs.csv
    tables/paired_seeds.csv
    tables/cases.csv
    tables/coverage.csv
    tables/regressions.csv
    tables/runtime_regressions.csv
    tables/levels.csv
    tables/groups.csv
    summary.json
    report.md
    plots/opt-2/*.{svg,png}
    plots/opt-3/*.{svg,png}
    plots/levels/*.{svg,png}
    circuits/                            # Optional diagnostic QPY and layout metadata
    checksums.sha256
<results-root>.lsf/                       # Scheduler ledger, logs, outcomes, diagnostics
```

Stages are `smoke`, `quality`, `followup-quality`, and `full`. Use filesystem-safe hashed panel/case IDs with mappings in the manifests. Keep levels, revisions, seeds, and attempts in distinct paths. Only the final collector writes aggregate tables.

### 8.1 Raw data contract

- **`manifest.json` / `profile.json`:** schema version, run/profile identity, explicit Qiskit and harness SHAs, Benchpress/plugin/patch/lock/input/config hashes, wheel/extension provenance, seed list, level list, single-call execution mode, runtime measurement contract, thread/trial settings, resource limits, and immutable schedule. Preserve archived manifests when reporting older untimed sessions.
- **`panel.jsonl` / `cases.jsonl`:** full inventory, exact node IDs, circuit/HamLib identities, input hashes/qubits, topology/target/config fingerprints, selection status, and exclusion reasons. Panel entries expand to one case per level.
- **`run.json`:** one scheduled invocation's identity, exact command/relevant environment, LSF host/job/index/attempt, exit status, reason, and artifact paths. UTC start/end timestamps may support job recovery and timeout handling; no elapsed-performance field is required. The controller synthesizes records for killed or unstarted jobs.
- **`quality.json`:** adapter-produced quality/runtime result with schema version, full identity, execution mode `quality`, single-call evidence, validation status, the input/output quality fields from `benchmark.extra_info`, and top-level `runtime_s`, `runtime_method`, and `runtime_scope`. No upstream benchmark statistics or QASM load timing. Preserve partial metadata on failures; mark it ineligible for successful-pair aggregation. Missing timing fields in older records must not invalidate their quality data.
- **`junit.xml` / logs:** native test failure diagnostics. Incidental pytest/LSF duration fields stay diagnostic and are not extracted into scientific tables.

Use UTF-8, explicit schema versions, integer counts, floating-point runtime seconds, JSON `null` and empty CSV cells for unavailable metrics, and a separate reason field. Zero gates/depth are valid values; successful runtime samples must be positive and finite.

### 8.2 Comparison tables and report

`runs.csv` contains every scheduled first-attempt invocation, including failures and timeouts:

```csv
run_id,profile,stage,panel_id,case_id,node_id,suite,topology,optimization_level,lsf_job_id,array_index,host,attempt,revision,qiskit_sha,config_hash,target_hash,seed,status,reason,input_qubits,output_qubits,native_2q_gate,gate_count_2q,depth_2q,runtime_s,runtime_method,runtime_scope,runtime_status,runtime_reason,raw_artifact_dir
```

`paired_seeds.csv` has one row per case/seed with paired status and quality/runtime changes:

```csv
profile,stage,panel_id,case_id,optimization_level,seed,pair_status,baseline_gate_count_2q,candidate_gate_count_2q,gate_count_change,gate_count_change_pct,baseline_depth_2q,candidate_depth_2q,depth_change,depth_change_pct,baseline_runtime_s,candidate_runtime_s,runtime_change_s,runtime_ratio,runtime_change_pct,runtime_status
```

A complete quality pair has one passing result from each revision in the same element attempt. A complete quality case has all configured seeds paired. Retain incomplete cases separately; cross-level quality comparisons require both levels complete. `runtime_status` independently distinguishes `available`, `missing`, `invalid`, and `not_applicable` on runs; `paired`, `missing`, `invalid`, and `ineligible` on seed pairs; and `complete` or `incomplete` on cases. `cases.csv` summarizes seeds and records runtime-paired seed counts. `levels.csv` compares L3/L2 quality and eligible runtime within each revision. `groups.csv` and `summary.json` include runtime coverage and aggregates; `runtime_regressions.csv` lists complete cases with runtime increases, while `regressions.csv` retains its quality meaning.

| Profile, levels 2 and 3 | LSF elements | Invocation rows | Paired-seed rows | Case rows |
| --- | ---: | ---: | ---: | ---: |
| Main quality, 50 entries / 3 seeds | **150** | **600** | **300** | **100** |
| Main quality, `N` entries / 3 seeds | `3N` | `12N` | `6N` | `2N` |
| Full, `N` entries / 5 seeds | `5N` | `20N` | `10N` | `2N` |

Preserve canonical first-attempt rows and separate retry history. Scheduled but unstarted invocations count as `not_run`; exclusions before scheduling remain outside the selected denominator. Build smoke checks and optional follow-ups do not increase main-run counts.

The Markdown report includes exact scope/seeds/levels, provenance, separate quality/runtime coverage, gate-count/depth/runtime comparisons by suite/topology, wins/ties/losses, largest quality changes and runtime slowdowns, and links to raw artifacts. State seconds, ratio direction, measurement scope, and the single-call timing limitations. Provide baseline-versus-candidate gate/depth/runtime plots, topology summaries, and cross-level plots as SVG and PNG. Runtime plots use only eligible timing pairs and label seconds or percentage changes explicitly. Preserve quality plots for historical sessions with no timing and state that runtime is unavailable; do not invent timing plots or forecast artifacts.

Save QPY only for explicitly investigated cases, retaining layout metadata and replay identity. Verify replay metrics match the original quality result. Publish the final scientific summary to `design/benchpress_comparison_results.md` only in a later explicit publication step.

## 9. Implementation deliverables and completion checklist

Implemented entry points (see the [usage guide](../tools/benchpress/README.md) for submission and local checks):

- `tools/benchpress/prepare.py`: immutable inputs, release builds, dependency locks, validation, and provenance.
- `tools/benchpress/adapter.py` and `quality_plugin.py`: deterministic configuration/seeds, single-call mode and timer, quality/runtime JSON exporter, and metadata.
- `tools/benchpress/lsf/`: launcher, manager, scheduler, ledger, worker context, and status/stop/resume controls based on the supplied reference.
- `tools/benchpress/run.py`: isolated child execution, watchdogs, and raw outcomes for each element.
- `tools/benchpress/compare.py`: quality/runtime pairing, independent coverage, CSV/JSON/plots, and report.

Preserve existing `.gitignore` edits. Keep bulky wheels/checkouts/raw data in the run directory with an intentional ignore rule before execution.

- [ ] Both Qiskit source hashes and the separate harness SHA are verified; Linux wheel imports resolve correctly.
- [ ] Dependencies, adapter, data, configurations, and targets are pinned and archived.
- [ ] Affected tests, adapter smoke checks, and small semantic/reproducibility checks pass.
- [ ] The same fixed 50-entry panel runs at both levels with seeds 0–2.
- [ ] Each successful invocation calls the transpilation callable exactly once and exports quality and callable runtime without benchmark calibration or repetitions.
- [ ] One main array follows build/validation directly; concurrency and deadline handling are verified.
- [ ] No shared output/config/skipfile contamination; all scheduled outcomes are accounted for.
- [ ] Reports contain gate count, two-qubit depth, callable runtime, independent coverage, and separate quality/runtime regressions with reconciled row counts.
- [ ] Historical untimed artifacts retain quality results with unavailable runtime; invalid samples and failed/incomplete pairs cannot enter runtime aggregates.
- [ ] All raw quality/provenance artifacts and first-attempt failures are preserved.
