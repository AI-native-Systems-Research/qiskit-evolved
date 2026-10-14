# Benchpress plan: SABRE fusion and wire-wait scoring versus baseline

Prepared on 2026-10-14. This document specifies a future benchmark run; no builds or benchmarks were executed while preparing it.

**Recommended first run: the `screening` profile.** Pilot 10 representative circuit/target cases, then run up to 50 cases with **three transpiler seeds, one process repetition per revision/seed, and at least three timed rounds per process**. This is at most **300 screening jobs**, with a **three-hour combined pilot/screening budget**, excluding setup, builds, validation, and analysis. Use pilot wall times to reduce the case count if necessary. The full-corpus profile remains an optional later experiment; it is not the default.

## 1. Comparison and questions

Compare these exact commits from this repository:

| Role | Commit | Description |
| --- | --- | --- |
| Baseline | `1298f960b7e814cfcb6aa9338b548ec5c6d2b355` | Parent of the SABRE change; currently also local `main` and `origin/main`. |
| Candidate | `90ccaac29b2a547592e870797265007a128a02eb` | Current branch `sabre-choose-best-swap-heuristics`: “Include gate fusion and wire wait time in Sabre swap scoring”. |

The proposed baseline really is **one commit before the candidate**. Pin these hashes rather than resolving `HEAD^` again when executing the plan. The implementation changes are in `crates/transpiler/src/passes/sabre/dag.rs` and `route.rs`; the other five changed files update tests. Both revisions report Qiskit `2.6.0.dev0`, so the package version alone cannot identify them.

Answer three questions:

1. Does the candidate reduce final native two-qubit gate count and two-qubit depth after transpilation and optimization?
2. What compilation-time overhead or improvement accompanies the quality change?
3. Are results consistent across circuits, connectivity, and transpiler seeds, without new correctness failures?

Raw SWAP count is a diagnostic, not the primary quality measure. This change explicitly anticipates fusion during later synthesis; a route with more SWAPs can produce fewer native gates. The wire-ready score estimates depth rather than calibrated hardware execution time. Do not report a hardware speedup from that score or from circuit depth alone.

## 2. Verified Benchpress behavior

Use [Qiskit/benchpress](https://github.com/Qiskit/benchpress) at inspected commit `b695f30e83a32bac05b9b4d8e98d37ba9aae5236` for both revisions.

| Finding | Consequence for this experiment |
| --- | --- |
| Benchpress runs directly with pytest; its Qiskit tests live in `benchpress/qiskit_gym`. | Run from the Benchpress checkout with the selected environment's Python. |
| Relevant suites are `device_transpile` and `abstract_transpile`. | Make these the comparison; construction/manipulation suites are optional controls. |
| Defaults are optimization level 2, `fake_torino`, and abstract topologies `all-to-all`, `square`, `heavy-hex`, `linear`. | Keep level 2 and the default CZ basis for the main experiment. Device tests use their backend's native basis. |
| Transpilation tests time `pm.run(circuit)`; circuit loading and pass-manager creation happen outside that timer. | Label the timing “pass-manager execution”, not total application startup or compilation including setup. |
| Output metrics include `extra_info.output_gate_count_2q`, `output_depth_2q`, `output_circuit_operations`, and `output_num_qubits`. | Extract these alongside timing statistics from benchmark JSON. |
| Those count/depth metrics filter on the backend's single native entangling gate, such as CZ. | Record the gate name; do not assume the metric always counts CX or recursively covers control-flow blocks. |
| The relevant pass-manager calls omit `seed_transpiler`. `FlexibleBackend` also omits a seed when constructing `GenericBackendV2`. | Apply the same small reproducibility patch to the shared Benchpress checkout before either run. |
| The Qiskit requirements use a custom `pytest-benchmark` Git fork. | Install that fork at a fixed SHA, not the stock package or a moving branch. |
| Validation checks supported gates and connectivity. | Keep it enabled, and supplement it with small semantic checks; a passing benchmark is not an equivalence proof. |

These observations come from the pinned [device tests](https://github.com/Qiskit/benchpress/tree/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/device_transpile), [abstract tests](https://github.com/Qiskit/benchpress/tree/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/abstract_transpile), [configuration](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/default.conf), [metrics implementation](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/utils/io.py), and [validator](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/utils/validation.py).

## 3. Isolated source trees and release builds

Use two detached worktrees, two runtime environments, and separate wheel and Rust build directories. Leave the development checkout's existing `.gitignore` changes and untracked `design/` content intact. The commands below are an execution recipe, not actions already performed.

Prerequisites: the same Python 3.12 interpreter for both environments, a C toolchain, and the Rust toolchain selected by `rust-toolchain.toml` (`1.89`). Qiskit declares Python >=3.10 and Rust >=1.89; Python 3.12 is the proposed common choice, subject to the dependency smoke test.

```bash
REPO=/Users/ophir/PycharmProjects/qiskit-evolved
RUN_ROOT="$REPO/.benchpress-work/2026-10-14-screening"
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

Prepare one patch against the Benchpress checkout and save it as `results/benchpress-reproducibility.patch`. Apply it once and reuse the resulting checkout for both environments. This patch is still to be implemented.

1. Add `seed_transpiler=int(os.environ["BENCHPRESS_TRANSPILE_SEED"])` to every `generate_preset_pass_manager` call in the three device and two abstract transpilation files. Add the necessary imports. Keep the optimization level and default layout/routing choices unchanged.
2. In `benchpress/utilities/backends/flexible_backend.py`, pass `seed=int(os.environ["BENCHPRESS_BACKEND_SEED"])` to the `GenericBackendV2` superclass constructor. Keep `noise_info` unchanged. A NumPy global seed alone will not control its independently constructed `default_rng`.
3. Add seed and configuration identifiers to the output metadata. Preserve the existing metrics and validation. Make missing seed variables fail clearly rather than silently restoring randomness.
4. Sort collected node IDs in a pytest collection hook, then save the exact ordered selection. Reuse it for both revisions. This avoids filesystem-order differences in Feynman/QASMBench discovery.

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
```

This defines the primary single-thread experiment. `QISKIT_NUM_PROCS=1` does **not** force the preset pipeline to use one SABRE trial: at this revision, the level-2 default layout and routing plugins explicitly use 20 trials. Keep those counts equal and record them. Direct `SabreSwap`/`SabreLayout` diagnostics must set trial counts explicitly. A later throughput experiment can use a fixed Rayon thread count, such as 4, and must be reported separately.

Copy and hash the exact `default.conf` for every configuration. Benchpress reads this file relative to its package; do not assume it has a general `--config` CLI override. Keep the default config for the primary run. Change the Qiskit optimization level to 3 only for a separate follow-up, and restore the original before changing configurations again.

Use the packaged fake Torino backend without refreshing it from a service. Save hashes of its packaged configuration/properties and the runtime package version. For abstract targets, save a canonical fingerprint of qubit count, sorted edges, operation names, gate errors, and durations. Confirm target fingerprints match between revisions. These runs need no hardware jobs or IBM account credentials.

## 5. Circuits, seeds, and staged workload

### 5.1 Circuit inventory

A **case** is one circuit on one target at one optimization level and thread configuration. Running the same circuit on four abstract topologies creates four cases. Seeds and timing repetitions do not create new circuit cases.

The following inventory is the **available corpus for selection**, not the default execution scope. The screening profile selects up to 50 cases from it; the optional full profile attempts the eligible corpus. Counts were checked against the pinned Benchpress source tree and HamLib input data; they are source inventory counts, not a claim that pytest has collected or successfully executed them yet.

| Suite / input | Circuits | Targets per circuit | Cases before exclusions | What it exercises |
| --- | ---: | ---: | ---: | --- |
| Device Summit: `device_transpile/test_summit.py` | 9 | Fake Torino | 9 | Large structured circuits, random two-qubit interactions, and simplification controls. |
| Device Feynman: all `.qasm` files directly under `benchpress/qasm/feynman/` | 53 | Fake Torino | 53 | Reversible arithmetic, multi-controlled gates, QFT, error correction, and other algorithmic circuits. |
| Device HamLib: `benchpress/hamiltonian/hamlib/100_representative.json` | 100 | Fake Torino | 100 | Hamiltonian evolution circuits from chemistry, condensed matter, and optimization problems. |
| Abstract QASMBench small: `benchpress/qasm/qasmbench-small/` | 42 | Four topologies | 168 | Small algorithms, entanglement, variational circuits, and correctness/control-flow examples. |
| Abstract QASMBench medium: `benchpress/qasm/qasmbench-medium/` | 23 | Four topologies | 92 | Arithmetic, QFT, quantum memory, classifiers, and state preparation. |
| Abstract QASMBench large: `benchpress/qasm/qasmbench-large/` | 58 | Four topologies | 232 | Larger instances of the same families plus Quantum Volume, QUGAN, and UCCSD. |
| Abstract HamLib: the same 100 representative Hamiltonians | 100 | Four topologies | 400 | How Hamiltonian circuits respond to different connectivity constraints. |
| **Total** | | | **1,054** | **162 device cases + 892 abstract cases.** |

The four abstract topologies are `linear`, `square`, `heavy-hex`, and `all-to-all`; each uses the default `['id', 'sx', 'x', 'rz', 'cz']` basis. The primary run uses optimization level 2 and the single-thread configuration from section 4. Construction and manipulation benchmarks are outside this inventory. Level 3 and multithreading each add a separate experiment and are excluded from the counts below.

The proposed 50-case panel includes these nine Summit tests, subject to the pilot budget adjustment below:

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

The dataset suites provide the following inputs. Screening uses the selections in section 5.2; only the optional full profile attempts every eligible input:

- **Feynman:** includes `adder`, `adder_8`, `bigadder`, `gf2^4_mult` through larger finite-field multipliers, `barenco_tof_*`, `tof_*`, `qcla_*`, `grover_5`, `qft`, `qft_4`, `qec`, and `teleport`. Each `.qasm` filename defines an input.
- **QASMBench small:** examples include `adder_n4`, `adder_n10`, `qft_n4`, `qaoa_n3`, `qaoa_n6`, `hhl_n7`, `ising_n10`, and `vqe_uccsd_n4/n6/n8`.
- **QASMBench medium:** examples include `bigadder_n18`, `bv_n14`, `qft_n18`, `qram_n20`, `ising_n26`, `multiplier_n15`, `swap_test_n25`, and `wstate_n27`.
- **QASMBench large:** includes QV inputs in `QV_n32/32.qasm` and `QV_n100/100.qasm`, `adder_n28/n64/n118/n433`, `qft_n29/n63/n160/n320`, `ising_n34/n42/n66/n98/n420`, and larger BV, GHZ, classifier, QUGAN, and W-state instances. Expand the slash-separated names into separate inputs. Follow the loader's rule excluding filenames containing `transpiled`.
- **HamLib:** 35 chemistry, 35 condensed-matter, 15 binary-optimization, and 15 discrete-optimization records. These include molecular electronic/vibrational problems, transverse-field Ising and Heisenberg models, Bose/Fermi Hubbard models, graph-coloring, SAT, and travelling-salesperson instances. The 100 records span **2–930 input qubits**. If the resolved Fake Torino has 133 qubits, 81 HamLib records fit and 19 are expected capacity skips in the device suite. Abstract targets are sized to the circuit and have no such 133-qubit limit, although large cases may exceed the resource budget.

The input rules are defined by the pinned [Feynman test](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/qiskit_gym/device_transpile/test_feynman.py), [QASMBench parameterization](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/workouts/abstract_transpile/qasmbench.py), and [HamLib data](https://github.com/Qiskit/benchpress/blob/b695f30e83a32bac05b9b4d8e98d37ba9aae5236/benchpress/hamiltonian/hamlib/100_representative.json). The exact executed membership will be saved in `results/cases.jsonl`, including full pytest node IDs, source paths or Hamiltonian record identities, input hashes, topology, and exclusion reasons. Reconcile the 1,054 expected cases with collection before running; investigate discrepancies instead of silently changing the denominator.

### 5.2 Recommended 50-case panel and 10-case pilot

Select the panel before examining candidate improvements. The proposed allocation is:

| Family | Selected inputs | Targets | Cases |
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

Pilot exactly these **10 cases on both revisions**, with seed 0 and one process repetition: the Summit SU2-89 and QV-100 cases; Feynman `gf2^8_mult`; the device chemistry HamLib case; small QASMBench `qaoa_n6` on `linear`; medium `qft_n18` on `square`; large `qft_n29` on `heavy-hex` and `swap_test_n41` on `linear`; abstract chemistry HamLib on `all-to-all`; and abstract condensed-matter HamLib on `heavy-hex`. This makes **20 pilot jobs** and covers every suite/size group and all four topologies. It does not pilot the full 1,054-case corpus.

Use pilot elapsed times and input size/structure to estimate the unpiloted cases. Mark these extrapolations explicitly. Remove or defer expensive cases if the panel does not fit the time budget, using the slower revision's cost for each observed pair. Preserve at least one case from each row of the allocation and all four abstract topologies where feasible; favor retaining complete topology groups. Record omissions and their cost basis in the manifest. Freeze the final `N <= 50` cases before inspecting quality/speed differences. A timed-out or failing selected case remains visible as an outcome even when deferred. If the budget cannot cover the minimum diversity, report a partial pilot instead of claiming a representative screen.

### 5.3 Seeds and repetitions per case

| Quantity | Smoke / pilot | Screening (default) | Full comparison (optional) |
| --- | --- | --- | --- |
| Transpiler seeds | `0` | **`0, 1, 2` — three seeds per case** | `0, 1, 2, 3, 4` |
| Process repetitions per seed and revision | 1 | **1**, indexed `0` | 3, indexed `0, 1, 2` |
| Revisions | Baseline and candidate | Baseline and candidate | Baseline and candidate |
| Minimum timed rounds per process | 3 | **3** | 3 |
| Process jobs per selected case | 2 | **6 = 3 seeds × 1 repetition × 2 revisions** | 30 = 5 seeds × 3 repetitions × 2 revisions |

Every baseline/candidate pair uses the same transpiler seed, backend seed `424242`, and input circuit. Quantum Volume's input seed remains `12345`; it is not another seed sweep. Timed rounds within a process measure timing variability; they are not independent transpiler seeds or independent process starts. SABRE's internal trial budget remains unchanged and is distinct from these experiment seeds.

For `N` screening cases, expect **`6N` job outcomes** and at least **`18N` timed rounds if all jobs succeed**: 50 cases give **300 jobs and at least 900 timed rounds**, plus 20 separate pilot jobs. The full profile instead produces `30N` job outcomes and at least `90N` timed rounds if successful; scheduling all 1,054 inventory cases would require 31,620 jobs. Capacity skips and resource exclusions reduce measured totals. Calibration, timeout preflight, and the final result-producing invocation add calls beyond those minimum rounds. Preserve actual round/iteration counts. Smoke and pilot results are not reused as screening/full-run repetitions.

### 5.4 Validation and execution stages

First run each revision's own affected Qiskit tests using its release wheel and the test dependencies declared in its `pyproject.toml`: `test_sabre_swap`, `test_sabre_layout`, `test_preset_passmanagers`, `test_transpile_layout`, and compiler `test_transpiler`. Collect the tests from that revision in a directory that does not shadow the installed wheel. Recheck import paths during this step. Candidate-specific expected routes must not be imposed on the baseline.

Next collect the two Benchpress suites in both environments and compare selected node-ID sets. Separate expected skips, such as circuits exceeding device capacity, from failures. Establish the following tiers before inspecting comparative results:

| Tier | Workload | Seeds / repetitions | Purpose |
| --- | --- | --- | --- |
| Smoke | Device `test_circSU2_89_transpile`; small abstract `qaoa_n6` on `linear` | Seed 0, one process each | Check compatibility, metrics, target identity, and the adapter. |
| Pilot | The 10 cases specified in section 5.2 | Seed 0, one process per revision; 20 jobs | Estimate cost and detect compatibility/resource problems within a 30-minute sub-budget. |
| Screening | Up to 50 cases from section 5.2, frozen after the cost pilot | Seeds 0–2, one process per revision/seed; up to 300 jobs | Obtain an initial quality/runtime comparison within the remaining three-hour budget. |
| Focused follow-up | Promising cases, regressions, or timing differences near noise | Ten seeds for quality, or three processes for timing, as specified below | Confirm findings on selected cases with a separate budget. |
| Full comparison | Eligible full corpus or a declared broader panel | Seeds 0–4, three processes per revision/seed | Optional broader validation after screening. |
| Configuration follow-up | Level 3 or fixed multithreading on a declared subset | Explicitly recorded seed/process policy | Separate sensitivity experiment. |

Treat `all-to-all` as a control and report it separately from constrained topologies. Screening should retain small, medium, and large circuit examples; archive reasons for every exclusion. Adjust the proposed panel using circuit family, qubit range, topology, and pilot cost, then freeze it before inspecting candidate-versus-baseline improvements. Do not select only winning circuits.

Example smoke commands, **after** building the wheels and applying the patch:

```bash
cd "$RUN_ROOT/benchpress"
"$BASE_PY" -m pytest --collect-only -q \
  benchpress/qiskit_gym/device_transpile \
  benchpress/qiskit_gym/abstract_transpile \
  > "$RUN_ROOT/results/collected-baseline.txt"

"$BASE_PY" -m pytest \
  benchpress/qiskit_gym/device_transpile/test_summit.py::TestWorkoutDeviceTranspile100Q::test_circSU2_89_transpile \
  --benchmark-min-rounds=3 --benchmark-max-time=1 \
  --benchmark-warmup=off --benchmark-save-data \
  --benchmark-json="$RUN_ROOT/results/smoke-baseline-device.json" \
  --junitxml="$RUN_ROOT/results/smoke-baseline-device.xml"

"$BASE_PY" -m pytest \
  benchpress/qiskit_gym/abstract_transpile/test_qasmbench.py::TestWorkoutAbstractQasmBenchSmall \
  -k 'qaoa_n6 and linear' --benchmark-min-rounds=3 --benchmark-max-time=1 \
  --benchmark-warmup=off --benchmark-save-data \
  --benchmark-json="$RUN_ROOT/results/smoke-baseline-abstract.json" \
  --junitxml="$RUN_ROOT/results/smoke-baseline-abstract.xml"
```

Repeat with `CAND_PY` and distinct `candidate` output names, including collection. Save stdout, stderr, command, and exit status for every invocation. The benchmark plugin still performs calibration and an additional result-producing invocation; disabling explicit warmup does not make this a cold-start measurement.

For screening, use one process per revision/seed with at least three measured rounds; the optional full profile uses three process repetitions. Execute pairs serially on one quiet, plugged-in machine, alternating baseline/candidate order by case, seed, and repetition. Within each seed, traverse a deterministic order that interleaves suites and topologies before moving to the next seed, so a budget stop does not only cover the cheapest family. Keep the backend seed fixed while varying only the transpiler seed. Do not run revisions concurrently or use pytest-xdist for measured runs. Record OS, CPU architecture/model, RAM, available cores, power conditions, and all thread settings. Check fixed-seed quality repeatability with a small untimed smoke replay; do not add process repetitions to every screening case for that purpose.

### 5.5 Runtime budget and profile settings

**Target a 2–3 hour pilot plus screening session, with a three-hour wall budget.** This limits benchmarking time, not the number of cases guaranteed to finish. There is no measured ETA yet. Setup, two release builds, the affected Qiskit tests/smoke checks, and report generation are outside this budget. Keep the previous unmeasured allowances of 1–3 hours for cold setup/builds and 0.5–2 hours for tests/smoke; cached builds may be quicker. The reduced smoke selection has two cases, or four baseline/candidate jobs.

Let `w` be the average **whole subprocess wall time** in seconds, including imports, collection, loading, target/pass-manager construction, preflight, calibration, timed rounds, validation, and serialization. Then, for serial execution:

```text
10-case pilot time = 2 × 10 × w = 20w
screening time    = 2 × N × 3 seeds × 1 repetition × w = 6Nw
full-profile time = 2 × N × 5 seeds × 3 repetitions × w = 30Nw
total time        = setup + validation/smoke + pilot + selected profile + analysis
```

These scenarios illustrate the scale; **none is a benchmark result or a measured prediction for this machine**:

| Average job wall time `w` | 10-case pilot | 50-case screening | Optional full profile, 1,054 cases |
| --- | ---: | ---: | ---: |
| 5 seconds | 1.7 minutes | 25 minutes | 43.9 hours / 1.8 days |
| 30 seconds | 10 minutes | 2.5 hours | 263.5 hours / 11.0 days |
| 120 seconds | 40 minutes; exceeds pilot sub-budget | 10 hours; reduce panel | 1,054 hours / 43.9 days |

Use actual **elapsed job time**, not `stats.median`, to forecast the remaining schedule. Benchpress can load the 41 MB HamLib file during collection, and process startup is paid again for every node. The large cases will not cost the same as small ones. Sum per-case estimates, including expected skip and timeout costs, instead of extrapolating only from the small smoke circuits.

Use a maximum 30-minute pilot sub-budget, counted inside the three-hour session. After the pilot, choose `N <= 50` such that the summed screening estimate plus a **25% scheduling reserve** fits the remaining time. For a uniform-cost illustration, `N <= floor((10800 - pilot_elapsed_s) / (1.25 × 6 × w))`, capped at 50. At 30 seconds/job and a 10-minute pilot, this gives at most 45 cases before accounting for topology-group selection. The reserve is a planning choice, not a confidence interval. Publish observed costs, extrapolations, selected count, and exclusions in `results/runtime_estimate.json`. Update remaining-time estimates during execution without silently changing the frozen selection.

The runner must stop admitting new baseline/candidate pairs when the remaining stage/session budget cannot cover both job timeouts plus shutdown allowance. Finish an admitted pair within that reserved time. Enforce an outer deadline as well; if a process overruns, terminate its process group and preserve timeout/incomplete-pair status. Mark scheduled jobs that never start as `not_run` with reason `budget_exhausted`. Do not turn incomplete seed coverage into a complete screening result or start the full profile automatically. Budgets reset only for a separate, explicitly selected follow-up run.

The following is the **proposed configuration contract for the future runner**, not an implemented CLI or an existing config file:

```json
{
  "profile": "screening",
  "candidate_case_limit": 50,
  "pilot_case_count": 10,
  "transpiler_seeds": [0, 1, 2],
  "process_repetitions": 1,
  "benchmark_min_rounds": 3,
  "benchmark_max_time_s": 1,
  "session_budget_s": 10800,
  "pilot_budget_s": 1800,
  "planning_reserve_fraction": 0.25,
  "preflight_timeout_s": 60,
  "job_timeout_s": 300,
  "backend_seed": 424242
}
```

### 5.6 Focused follow-up policy

Keep the initial screening report intact. Use a separate run/profile and budget to confirm selected findings:

- **Quality:** select up to ten cases with consistent improvement, material gate/depth regression, or seed-sensitive tradeoffs. Run a new paired experiment over seeds `0–9` with one process per revision/seed and at least three timed rounds: 20 jobs per case, including fresh executions of seeds `0–2`. Both improving and regressing cases remain eligible. Record the selection reason and do not pool this selected set into an unbiased whole-corpus average.
- **Timing:** for changes near noise or the 5% review threshold, run seeds `0–2` with three process repetitions per revision/seed on the affected cases: 18 jobs per case. Compare process-level variation before claiming a small runtime change; within-process rounds alone do not measure startup/process variability.
- **Correctness/timeouts:** investigate any new failure first, preserving the original outcome. Larger limits belong to a separate diagnostic configuration.
- **Expansion:** use the optional full profile (five seeds, three process repetitions) only when broader evidence is needed. Level 3 and multithreading remain separate sensitivity experiments.

Give each focused follow-up a declared budget, initially one hour, and apply the same pair/deadline policy. A ten-case quality follow-up has 200 jobs and would take about 100 minutes at 30 seconds/job, so it would require fewer cases or a separately chosen larger budget. Do not automatically append follow-up work to the three-hour screening session.

## 6. Timeouts, coverage, and correctness limits

The pinned [plugin fixture](https://github.com/1ucian0/pytest-benchmark/blob/e33f5934cb462e719b13b8d352ced107f575b4f7/src/pytest_benchmark/fixture.py) implements `--timeout-skip-list` as a **preflight invocation** in a subprocess. It does not cap the later calibration, measured rounds, or final invocation. Likewise, `--benchmark-max-time=1` is a timing budget used to choose rounds, not a one-second hard limit on a test.

For the pilot and screening run, implement a small external runner that launches one selected node ID per pytest subprocess. Use **60 seconds for the plugin's preflight and 300 seconds for the entire node process**, equally for both revisions, plus the stage/session deadlines from section 5.5. The optional full profile can use 120/900 seconds, recorded separately. Fix limits before measuring; changes create a new configuration rather than overwriting earlier outcomes. Use a process-group watchdog so a stuck Rust call or subprocess cannot evade the cap. Archive an explicit timeout record if the child terminates before writing JSON/XML. One process per node adds startup cost, which stays outside `pm.run` timing.

The fork writes `skipfile.txt` in the current directory and consults it on later invocations, even without the timeout option. Its implementation reports matching entries as failures. Archive and move any generated skipfile after each child, and require an empty starting state for the next child. Never let a baseline timeout silently determine candidate coverage or vice versa. Preserve the timeout/failure as an outcome even if a later investigation succeeds. A frozen resource-exclusion manifest is separate from this automatic skipfile.

Keep the normal Benchpress gate/connectivity validation. Add a small, untimed semantic check on suitable unitary circuits, accounting for initial/final layouts, ancillas, and global phase. Do not build dense operators for 89/100-qubit benchmarks. Treat measurement/control-flow examples separately; their correctness and recursive metrics need dedicated handling.

For attribution, run an untimed pass-manager callback on representative inputs to record whether `SabreLayout` or `SabreSwap` actually executes. A valid VF2 layout can avoid SABRE. Report unchanged no-routing cases as controls. If routing-only diagnostics are needed, use a separate fixed-layout `SabreSwap` experiment with an explicit seed/trial budget, followed by the same two-qubit synthesis on both revisions. Keep callback instrumentation and diagnostic pass changes out of the primary timing run.

## 7. Analysis and decision rules

Join by **profile/stage + node ID + configuration/target fingerprint + transpiler seed + repetition**. Do not join by list position, package version, or short test name. Check that both records passed before calculating a performance ratio; keep unpaired errors, skips, timeouts, budget-exhausted jobs, and missing results in a separate coverage table.

Extract these fields into a flat CSV:

| Field | Source / interpretation |
| --- | --- |
| Full identity | Qiskit commit, wheel/extension hash, Benchpress and plugin commits, patch hash, dependency lock hash, target/config hash, node ID, seeds, repetition |
| Runtime | `benchmarks[].stats.median`, plus mean, spread, rounds, iterations, and raw timing data |
| Quality | `benchmarks[].extra_info.output_gate_count_2q` and `output_depth_2q` |
| Context | Native two-qubit gate name, input/output width, operation histogram, topology, family, optimization level, thread/trial settings |
| Outcome | Passed, skipped, failed, timeout, or missing artifact; preserve the reason and process exit status |

For any positive baseline metric, report `100 * (candidate / baseline - 1)`; negative means an improvement for all three primary metrics. Also report absolute gate/depth differences. When the baseline is zero, use the absolute difference and a separate zero-baseline category; never divide by zero or discard those cases silently.

For screening, use each process's median timed round as the timing observation for that case/revision/seed. For profiles with multiple process repetitions, first take the median across their process-level observations. Summarize paired seed results per circuit before computing aggregate statistics, so repetitions and seeds do not masquerade as independent circuits. Report geometric-mean ratios for positive metrics, medians, win/tie/loss counts, and worst regressions, split by topology and circuit family. Use confidence intervals that resample whole circuit groups if reporting uncertainty. Screening has only three seeds and one process repetition; label its timing findings preliminary and confirm small changes with the focused timing follow-up. Do not hide quality regressions behind one combined score or let all-to-all controls dominate the constrained-topology conclusion.

Agree on these initial review rules before running:

- No new correctness failures. Any new timeout, crash, or missing benchmark is investigated and reported.
- Flag runtime slowdowns above 5% for repeat confirmation; treat this as a review threshold, not a claim of statistical significance.
- List every gate-count/depth regression, highlighting increases above 5% and giving absolute counts so small-circuit percentages remain interpretable.
- Claim an overall quality improvement only when the paired summaries support it and the compilation cost is stated. A gate-count win with a depth loss, or better quality with slower compilation, is a tradeoff to report explicitly.

The final report should contain a coverage table, aggregate results by family/topology, a per-case CSV, the largest improvements/regressions, and runtime-versus-quality scatter plots. Separate scientific findings from compatibility failures or limits on the sampled workload.

## 8. Output format and artifacts

Deliver both a human-readable report and machine-readable data. All examples below specify future output paths; these results do not exist yet. Use a distinct `RUN_ROOT` for every profile/run, such as `2026-10-14-screening` and a later `2026-10-15-followup-quality`, to preserve manifests and earlier results.

```text
design/
  benchpress_comparison_results.md        # Final report and links to retained artifacts
.benchpress-work/2026-10-14-screening/
  build-requirements.lock
  shared-requirements.lock
  baseline.freeze.txt
  wheels/{baseline,candidate}/            # The two locally built Qiskit wheels
  results/
    manifest.json                        # Run identity, versions, machine, flags, hashes
    profile.json                         # Seeds, repetitions, budgets, timeout policy
    cases.jsonl                          # Complete inventory, selected cases, exclusions
    runtime_estimate.json                 # Pilot-based forecast and its assumptions
    benchpress-reproducibility.patch
    configs/                             # Exact config and target fingerprints/snapshots
    build-{baseline,candidate}.log
    validation/                          # Qiskit tests and small semantic-check results
    raw/<stage>/<case-id>/<revision>/seed-<s>/repeat-<r>/
      run.json                           # Identity, status, timestamps, wall time, exit code
      benchmark.json                     # Native pytest-benchmark JSON, if produced
      junit.xml                          # Pytest outcomes and failure details, if produced
      stdout.log
      stderr.log
      skipfile.txt                       # Archived only if the plugin generated one
    tables/
      runs.csv                           # One row for every scheduled process job
      paired_seeds.csv                   # One row per case/seed after repetition reduction
      cases.csv                          # One row per case, across the profile's seeds
      coverage.csv                       # Inventory, exclusions, all outcomes by revision
      regressions.csv                    # Quality/runtime regressions and investigation flags
    summary.json                         # Aggregate counts, metrics, uncertainty, caveats
    plots/
      runtime_vs_gate_count.{svg,png}
      runtime_vs_depth.{svg,png}
      quality_by_topology.{svg,png}
    circuits/                            # Optional input/output QPY for investigated cases
    checksums.sha256                     # Integrity hashes for the completed artifact bundle
```

`stage` is `smoke`, `pilot`, `screening`, `followup-quality`, `followup-timing`, or `full`. The profile and stage determine seed/repetition expectations. `revision` is `baseline` or `candidate`. Use a filesystem-safe `case-id` derived from the full node ID plus input and configuration hashes, with its mapping retained in `cases.jsonl`; raw Hamiltonian node IDs can contain slashes and other unsuitable filename characters. Keep files from different seeds, repetitions, and revisions separate.

The manual smoke commands in section 5 initially write flat `smoke-*.json/xml` files under `results/`. Retain those bootstrap artifacts; the implemented runner uses the structured `raw/` layout above.

### 8.1 Raw data contract

- **`manifest.json`:** schema version, run ID, profile, both Qiskit SHAs, wheel/extension hashes, Benchpress/plugin SHAs, reproducibility-patch hash, dependency locks, CPU/OS/RAM information, compiler/Python versions, selected seeds, repetition/round policy, thread/trial settings, timeouts, stage/session budgets, and the ordered job schedule. Archive the effective configuration in `profile.json`, including any pilot-based reduction in selected cases.
- **`cases.jsonl`:** one JSON object per inventory case, including `case_id`, full `node_id`, suite/family, input path or full Hamiltonian record identity, input hash, input qubits, target/topology, config/target hashes, selection status, and any exclusion reason. Preserve dataset membership even when it is not executed.
- **`run.json`:** one record per job with case/revision/seed/repetition, exact command and relevant environment, UTC timestamps, `elapsed_wall_s`, exit status, outcome, failure/skip reason, and artifact paths. The controller writes this even when the child crashes or times out before producing benchmark JSON/XML. Include `not_run` for a scheduled job that never started; missing data must not appear as a pass.
- **`benchmark.json`:** retain the original benchmark statistics, raw timing data from `--benchmark-save-data`, and `extra_info` quality metrics. Do not replace it with only aggregate numbers. Add provenance in a namespaced metadata object or the accompanying `run.json`.

Use UTF-8, explicit schema versions in JSON/JSONL, seconds for timing, integer gate/depth counts, JSON `null` for unavailable values, and empty CSV cells for unavailable values. Preserve the reason in a separate column. A zero gate count is valid data; it is not a missing value.

### 8.2 Comparison tables and report

`runs.csv` contains one row per scheduled job, including failures and timeouts. Its core columns are:

```csv
run_id,profile,stage,case_id,node_id,suite,topology,revision,qiskit_sha,config_hash,target_hash,seed,repetition,status,reason,elapsed_wall_s,median_s,mean_s,stddev_s,rounds,iterations,input_qubits,output_qubits,native_2q_gate,gate_count_2q,depth_2q,raw_artifact_dir
```

`paired_seeds.csv` reduces the configured process repetitions for each revision (one for screening, three for the full/timing profiles), then joins baseline and candidate for the same profile/stage/case/seed. It records the number of expected and completed repetitions, baseline/candidate runtime and quality, ratios, percentage changes, absolute gate/depth changes, and pairing/instability flags. Its core comparison columns are:

```csv
profile,stage,case_id,seed,pair_status,expected_repetitions,baseline_repetitions,candidate_repetitions,baseline_median_s,candidate_median_s,runtime_change_pct,baseline_gate_count_2q,candidate_gate_count_2q,gate_count_change,gate_count_change_pct,baseline_depth_2q,candidate_depth_2q,depth_change,depth_change_pct
```

Require all configured process repetitions on both revisions for a complete seed pair: one each for screening and three each for the full profile. Where a fixed seed is repeated, flag any count/depth discrepancy rather than silently averaging it away. Keep incomplete pairs with unavailable comparison values. A case is complete only when all its configured seeds are paired; retain partial-case findings separately from complete-case aggregates.

| Profile / stage | `runs.csv` rows | `paired_seeds.csv` rows | `cases.csv` rows |
| --- | ---: | ---: | ---: |
| Pilot, 10 cases | 20 | 10 | 10 |
| Screening, `N` cases | **`6N`** | **`3N`** | **`N`** |
| Screening, all 50 proposed cases | **300** | **150** | **50** |
| Full profile, `N` cases | `30N` | `5N` | `N` |
| Quality follow-up, `K` cases | `20K` | `10K` | `K` |
| Timing follow-up, `K` cases | `18K` | `3K` | `K` |

These counts include explicit `not_run`/incomplete outcomes for scheduled work after a budget stop. Cases excluded before scheduling remain in `cases.jsonl` and `coverage.csv`, outside the selected `N`. Smoke/pilot/follow-up tables remain separately identified; they do not increase the screening denominator. Tag `cases.csv` with profile/stage, expected/paired seed counts, and `complete`/`partial` status.

The Markdown report will contain: scope and exact corpus size; seeds/repetitions and hardware; setup and measured elapsed times; passed/skipped/failed/timeout/not-run coverage; runtime, gate-count, and depth comparisons by suite/topology; win/tie/loss counts; the largest regressions and improvements; scatter plots; and a conclusion limited to completed measurements. Each notable case links to its comparison row and raw artifacts. Provide SVG plots for inspection/export and PNG copies for embedding.

Do not save every large transpiled circuit by default. For cases selected for semantic checks or regression investigation, save input/output `.qpy` files plus layout metadata in an untimed replay, tagged with revision and seed. Identify these as replay artifacts and verify their count/depth matches the measured result. The pinned inputs, hashes, configuration, and seeds remain the reproduction source for every other case.

## 9. Execution deliverables and completion checklist

Implement the following in a later execution task; this planning change creates only this Markdown file:

- `tools/benchpress/prepare.py`: verify pinned revisions, build/install isolated wheels, freeze dependencies, apply the shared reproducibility patch, and record provenance.
- `tools/benchpress/run.py`: support the proposed `screening`, focused-follow-up, and optional `full` profiles; select/freeze the panel, execute the cost pilot, enforce pair/stage/session budgets, run the seed/repetition matrix serially, manage watchdogs/skipfiles, and save JSON, XML, logs, and missing-result records. Default to the screening configuration; do not launch the full corpus implicitly.
- `tools/benchpress/compare.py`: validate pair identity and profile-specific row/seed/repetition expectations, extract quality and timing fields, retain partial results, produce CSV/plots, and render the comparison report.
- `design/benchpress_comparison_results.md`: measured conclusions, reproduction commands, artifact locations, and unresolved limitations.

Keep bulky wheels, checkouts, logs, and raw results under the run directory, with an intentional local ignore rule before execution. Do not overwrite the developer's existing `.gitignore` edits. Preserve all raw artifacts even when only the report and small scripts are committed.

- [ ] Both local release wheels and their exact source hashes are verified.
- [ ] Dependencies, Benchpress, plugin, patch, input corpus, config, and targets are pinned and archived.
- [ ] Affected Qiskit tests and both Benchpress smoke runs succeed, or blockers are documented.
- [ ] Fixed-seed quality repeatability is verified; node IDs and eligible corpus are frozen.
- [ ] The 10-case pilot produces an archived ETA and a frozen panel that fits the remaining screening budget with reserve.
- [ ] The screening profile uses three seeds, one process repetition, and at least three timed rounds; follow-ups/full runs are separately identified.
- [ ] Budget exhaustion stops new pairs and produces explicit partial/not-run outcomes without silently expanding the run.
- [ ] Paired single-thread runs finish with complete status accounting and no shared skipfile contamination.
- [ ] Analysis reports final native-gate count, two-qubit depth, runtime, coverage, and regressions.
- [ ] Raw job records, comparison tables, report, plots, and provenance are retained with reconciled row counts.
- [ ] Results support a bounded conclusion about this SABRE commit; unmeasured workloads and optional follow-ups remain explicit.
