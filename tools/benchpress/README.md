# Benchpress quality and runtime comparison on LSF

This harness implements the [comparison plan](../../design/benchpress_comparison_plan.md) and [LSF implementation plan](../../design/benchpress_lsf_implementation_plan.md). It compares **native two-qubit gate count, depth, and transpilation runtime**, with optimization levels **2 and 3**, seeds **0, 1, 2**, and the fixed **50 circuit/target entries**. One submission builds and validates both revisions, runs **150 array elements / 600 single-call invocations**, and generates reports. The default concurrency is **16 elements**. Runtime is recorded automatically around the existing transpilation call; no additional invocations or timing flags are needed.

The timer measures wall-clock seconds around `pm.run(circuit)` using `perf_counter`, excluding circuit loading, pass-manager construction, pytest startup, and validation. All profiles, including `full`, take one sample per revision/level/seed without warmup or calibration. Fixed baseline-first ordering and host load can affect these observations; runtime reports describe the measured single-call differences.

The baseline is `1298f960b7e814cfcb6aa9338b548ec5c6d2b355`; the candidate is `90ccaac29b2a547592e870797265007a128a02eb`. They remain independent of the harness commit. Continue on `sabre-choose-best-swap-heuristics-benchpress`.

## Submit

Commit the implementation files on the existing branch before submitting: the launcher archives a committed harness and refuses changed/untracked files under `tools/benchpress/`. Unrelated checkout changes are allowed. The commands below run on the cluster's Linux login node, after making that commit available there. Use shared paths visible to every compute host.

Required software: Python **3.12**, `git`, a C compiler, Rust **1.89.0** through `rustup`, and LSF clients `bsub`, `bjobs`, `bhist`, `bkill`, and `bparams`. The launcher reads LSF memory units from `$LSF_ENVDIR/lsf.conf` (falling back to `lsadmin showconf lim`, which login nodes usually refuse) and array limits from `bparams -l`; workers validate exclusive-core affinity and platform compatibility. `rusage` is a reservation, not an OS memory limit.

```bash
BP_REPO=/shared/PROJECT/USER/qiskit-evolved
BP_RESULTS=/shared/PROJECT/USER/benchpress-results/sabre-quality-o2-o3-001
BP_CONTROL=/shared/PROJECT/USER/benchpress-control

# Load the site's Python/compiler modules first, if needed.
python3.12 -m venv "$BP_CONTROL"
BP_PY="$BP_CONTROL/bin/python"

"$BP_PY" "$BP_REPO/tools/benchpress/lsf/submit.py" \
  --repo "$BP_REPO" \
  --harness-ref HEAD \
  --results-root "$BP_RESULTS" \
  --profile quality --opt-levels 2 3 --seeds 0 1 2 \
  --max-parallel 16 --queue normal --lsf-memory-unit GB \
  --task-mem-gb 8 --task-wall 0:30 \
  --run-timeout-s 300 --element-timeout-s 1500 --campaign-limit-min 180 \
  --build-slots 16 --build-mem-gb 64 --build-wall 6:00 \
  --manager-wall 24:00 \
  --dry-run
```

Replace the shared paths and queue for your site. Remove `--dry-run` from the same command to submit. Dry-run validates committed inputs and prints the effective matrix and initial `bsub` command, without creating a session or submitting jobs. Live cluster checks run on actual submission. Both the output directory and its `.lsf` sibling must be new.

Use `--job-env-script /shared/path/modules.sh` for a Bash script that loads modules and sets dependency cache locations on compute nodes. The launcher snapshots and hashes that script. The controller Python must remain accessible at its original shared path.

Run `submit.py --help` for all arguments. Resource, queue, timeout, and output arguments are described in the [argument table](../../design/benchpress_lsf_implementation_plan.md#5-submission-command-and-arguments). Additional implementation options:

| Argument | Purpose |
| --- | --- |
| `--benchpress-source PATH` | Local Git mirror containing the pinned Benchpress commit; archive before submission instead of fetching on the build node. |
| `--benchmark-source PATH` | Local Git mirror containing the pinned pytest-benchmark fork; archive before submission. |
| `--build-cache-root PATH` | Optional shared cache of release wheels, locked and keyed by source, platform/ABI, toolchain, build lock, Cargo lock, and adapter hash. Cache hits verify wheel hashes. Environments are always created at their final session paths. |
| `--panel-file PATH` | JSONL selection using collected `node_id` and `selection_reason`; optional input hashes must match. Set `--panel-size` to match a custom quality panel. |
| `--profile followup-quality` | Requires an explicit panel file; defaults to ten seeds. Reports remain separate from the main campaign. |
| `--profile full --campaign-limit-min N` | All device-compatible corpus entries; five seeds, 900-second children, 3900-second element limit, and 1:10 worker wall limit. Explicit campaign budget required. |

Large arrays are split at the cluster's `MAX_JOB_ARRAY_SIZE`; chunks run serially so the requested concurrency is global. Workers execute `L2/baseline`, `L2/candidate`, `L3/baseline`, `L3/candidate` on one allocated physical core, selecting one hardware thread and limiting numerical libraries to one thread. No CPU-count, CPU-model, or load selectors are added.

## Network and build preparation

By default the build job fetches the pinned upstream repositories and resolves Python dependencies once. Both Qiskit environments receive the same dependency versions; the candidate is installed with `--no-deps`. Build, runtime, and analysis locks, wheels, compiler versions, extension hashes, and source identities are retained. Each revision runs its own affected Qiskit tests, small semantic/reproducibility checks, collection checks, and single-call adapter checks before the array can start.

For offline compute nodes, supply both source mirror arguments. In the environment script set `PIP_NO_INDEX=1`, `PIP_FIND_LINKS` to a shared Linux/Python-3.12 wheelhouse, `CARGO_HOME` to a populated crate cache, and `CARGO_NET_OFFLINE=true`. Preinstall Rust 1.89.0. The wheelhouse must cover Benchpress requirements, Qiskit/runtime dependencies, the build systems for Qiskit and the benchmark fork, the validation dependencies in `prepare.py`, and matplotlib. Missing packages/crates fail build validation; the array is never submitted on a failed build. Do not use macOS wheels on Linux.

## Monitor and recover

```bash
"$BP_PY" "$BP_REPO/tools/benchpress/lsf/control.py" status --results-root "$BP_RESULTS"
tail -f "$BP_RESULTS.lsf/logs/manager-1.log"

"$BP_PY" "$BP_REPO/tools/benchpress/lsf/control.py" stop --results-root "$BP_RESULTS"
"$BP_PY" "$BP_REPO/tools/benchpress/lsf/control.py" reap --results-root "$BP_RESULTS"
"$BP_PY" "$BP_REPO/tools/benchpress/lsf/submit.py" --resume --results-root "$BP_RESULTS"
```

`stop` requests cancellation of this session's jobs, checking job ID, unique name, and owner. `reap` repeats that reconciliation for orphans. Neither uses broad cancellation. Failed queries or ambiguous submission responses remain unresolved; an absent job is reconciled with `bhist` or previously recorded terminal scheduler evidence. Do not manually resubmit an ambiguous job. Inspect `.lsf/attention.json`, the ledger, and diagnostics to establish what the scheduler accepted.

Resume requires every prior job to be confirmed terminal. It uses the archived code/settings, creates a new attempt and deadline epoch, and reruns all revision/level children of an incomplete element together. First-attempt rows remain canonical; retry rows are separate. An interrupted build is moved into `failed-builds/`; fresh environments are created at the original final paths, preserving failed-build diagnostics.

The three-hour campaign deadline starts with the first quality worker and includes subsequent queue waits. It is a resource cap, not an ETA. The manager cancels outstanding campaign jobs, confirms terminal states, and reports partial coverage when the deadline expires. Build and report jobs have separate limits. A hard manager termination may require `reap` followed by `resume`. To regenerate a partial report after `reap` confirms terminal states, run:

```bash
"$BP_RESULTS/env/analysis/bin/python" \
  "$BP_RESULTS/harness/tools/benchpress/compare.py" --results-root "$BP_RESULTS"
```

## Output

Start with **`$BP_RESULTS/results/report.md`**. Operational status is **`$BP_RESULTS.lsf/report.md`**.

- `results/manifest.json`: source/build/dependency/config/input hashes, platform, thread settings, seeds, levels, and immutable artifact checksums.
- `results/{inventory,panel,cases}.jsonl`, `tasks/<profile>.jsonl`: inventory, selected inputs, level expansion, and one-based array mapping.
- `results/raw/<stage>/<panel-id>/opt-<level>/seed-<seed>/attempt-<attempt>/<revision>/`: `run.json`, `quality.json`, JUnit, stdout/stderr, private working directory/cache. Killed/unstarted children receive synthesized records; interrupted records remain alongside them.
- `results/tables/`: `runs.csv`, `paired_seeds.csv`, `cases.csv`, `coverage.csv`, `regressions.csv` (quality), `runtime_regressions.csv` (complete-case slowdowns), `levels.csv`, `groups.csv`, and separate `retry_runs.csv`.
- `results/summary.json`, `plots/{opt-2,opt-3,levels}/*.{svg,png}`, `checksums.sha256`: aggregate quality/runtime data and figures, with independent runtime coverage.
- `.lsf/ledger.json`, `scripts/`, `logs/`, `diagnostics/`, `outcomes/`: submission intent, exact commands, scheduler responses, worker outcomes, and reconciled element records.

Comparisons require matching input/config/target/seed/revision identities and a common element allocation/attempt. A complete case requires all configured seeds. Zero gates/depth are valid; percentage changes with a zero baseline are blank. Failures and incomplete cases remain in coverage.

`quality.json` records `runtime_s`, `runtime_method`, and `runtime_scope`. Paired tables report baseline/candidate seconds, `runtime_change_s` (candidate minus baseline), `runtime_ratio` (candidate/baseline), and `runtime_change_pct`; negative changes or ratios below one mean faster. Cases use medians of paired per-seed changes and ratios. Group summaries report runtime-complete case counts, median changes, geometric-mean ratios, and faster/tie/slower counts. Level comparisons measure L3 relative to L2 on the common complete panel.

Runtime has independent validity and coverage: missing, nonpositive, nonfinite, or unsupported timings do not invalidate otherwise valid quality results. Failed invocations never contribute runtime comparisons. Older campaigns retain their quality results with unavailable runtime when processed by the updated collector; process timestamps, JUnit durations, and LSF accounting cannot reconstruct this measurement. To measure runtime, commit the updated harness and submit a **new results root** with the same command above. Resuming an old session uses its archived harness. Reports do not infer hardware execution speedups from compilation runtime or circuit depth.

## Local verification

No LSF installation or Qiskit build is needed for the core tests:

```bash
python3.12 -m venv /tmp/benchpress-tests
/tmp/benchpress-tests/bin/python -m pip install -r tools/benchpress/tests/requirements.txt
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 /tmp/benchpress-tests/bin/python -m pytest \
  tools/benchpress/tests -q -o addopts= --confcutdir=tools/benchpress/tests
```

The opt-in upstream integration test additionally needs installed `qiskit` and `qiskit-ibm-runtime`. Set `BENCHPRESS_TEST_SOURCE` to a local Git checkout containing the pinned Benchpress SHA and run `tests/test_upstream_integration.py`. It archives a fresh upstream tree, verifies the 1,054-entry inventory and 50-entry panel at both levels, executes the two adapter checks at both levels, and checks small-circuit semantics. This verifies adapter compatibility; only the Linux campaign builds and compares the pinned algorithm revisions.
