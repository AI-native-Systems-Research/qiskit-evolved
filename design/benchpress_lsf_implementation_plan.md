# Implementation plan: one Benchpress quality and runtime comparison on LSF

Prepared 2026-10-14. The implementation is now in [`tools/benchpress/`](../tools/benchpress/README.md). The commands below are implemented; local harness and upstream-adapter checks are described in that guide. **Linux builds of the pinned revisions and live LSF execution remain to be validated on the cluster. No cluster jobs were submitted.** Circuit selection and quality analysis are defined in [benchpress_comparison_plan.md](benchpress_comparison_plan.md).

## 1. Default run and source identity

Submit once: build/validate, run the fixed 50-entry circuit/target panel, then report. Run **optimization levels 2 and 3**, baseline and candidate, and seeds **`0,1,2`**. Each invocation uses a fresh process and calls the transpilation callable **once**. The campaign compares native two-qubit gate count, depth, and the wall-clock runtime of that single callable execution. Record runtime in seconds and compare paired baseline/candidate differences, ratios, and percentages.

There are **150 LSF array elements / 600 invocations**, with **16 elements running concurrently**. One element owns a circuit/target entry and seed, and runs all four level/revision combinations sequentially on the same host. The `quality` profile names this main experiment; it is not a preliminary stage.

| Identity | Value / rule |
| --- | --- |
| Current branch | `sabre-choose-best-swap-heuristics-benchpress`; continue implementation here. |
| Planning tip inspected | `ff29e0f5650f4ae3f0bf4ddb86c97d1bd5a6a12c` (`pptx + benchpress support`). |
| Baseline Qiskit | `1298f960b7e814cfcb6aa9338b548ec5c6d2b355`. |
| Candidate Qiskit | `90ccaac29b2a547592e870797265007a128a02eb`. |
| Harness | Resolve `--harness-ref HEAD` once, after implementation is committed, and archive that exact code. |
| Benchpress | `b695f30e83a32bac05b9b4d8e98d37ba9aae5236`. |
| pytest-benchmark fork | `e33f5934cb462e719b13b8d352ced107f575b4f7`, installed for fixture compatibility with upstream calibration/repetition disabled; the adapter times the single call. |

The planning commit changes only the plan and a presentation relative to the candidate. **Do not use current `HEAD^` as the baseline:** it is now the modified router. Further planning/implementation commits move that relationship again. Store baseline, candidate, and harness as independent identities. Validate `candidate^ == baseline` for this experiment; a different algorithm comparison requires explicit new pins.

The launcher verifies its selected harness commit contains the runner and profiles. Refuse uncommitted changes to executed implementation/profile files; unrelated local `.gitignore` edits can remain outside the snapshot. Build Qiskit from the explicit source pins, and preserve the current development branch/history.

## 2. LSF reference and cluster conventions

Use the supplied [LSF reference](</Users/ophir/PycharmProjects/qiskit-transpile-bench/lsf/README.md>) and [cluster guide](</Users/ophir/PycharmProjects/qiskit-transpile-bench/docs/cluster.md>) as design references. Inspected reference commit: `6e26346069833b87a4058b84ec9fcc17ef19ddeb`. That repository is a reference, not a runtime dependency or a file tree to modify.

| Reference | Apply here |
| --- | --- |
| `lsf/submit.py` | One launcher on the login node; validate arguments and shared paths. |
| `lsf/manager.py` | Manager orchestrates build/validation, the quality array, reporting, and recovery. |
| `lsf/scheduler.py` | Structured `bsub` arguments; accepted/rejected/ambiguous responses; `bjobs -json` and `bhist` reconciliation. |
| `lsf/context.py` | Capture allocation/CPU/affinity/queue and validate single-host resources. |
| `lsf/retry.py` | Durable submission intents and unique names prevent duplicate submission after ambiguous responses. |
| `lsf/job.py`, `logging.py`, `control.py` | Descendant cleanup, outcomes, logs/events, status/stop/reap/resume. |

Use a one-slot manager and a one-exclusive-core array worker. Retain `span[hosts=1]` and `affinity[core(1,exclusive=(core,alljobs))]`; record and validate the allocation. The example queue is `normal`; omission uses the site default. Use eligible queue hosts without added CPU-count, CPU-model, or load selectors. The reference's nine-core timing/noise monitor and automatic noise retries are outside this quality experiment.

The reference assumes **GB units** for `rusage[mem=...]`. Validate this convention on the live cluster, including multi-slot build-job accounting. `rusage[mem=8]` is a reservation under that convention, not a universal OS-enforced 8 GB limit. Confirm exclusive-core affinity support. Record effective resources; preserve raw artifacts rather than adopting the reference's bulk cleanup policy. IBM documents [array concurrency](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=o-j), [affinity requests](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=strings-affinity-string), and [memory units](https://www.ibm.com/docs/kk/spectrum-lsf/10.1.0?topic=lsfconf-lsf-unit-limits).

## 3. Task matrix and concurrency

Use these identities:

- `panel_id`: circuit/input identity plus target/topology.
- `case_id`: panel identity plus level/configuration/target fingerprint.
- `task_id`: stage + panel identity + seed; one array element.
- `invocation_id`: task + level + Qiskit revision + attempt; one pytest child.

Freeze `tasks/quality.jsonl` with contiguous **one-based** `array_index` values. Workers read `LSB_JOBINDEX` at execution time. `%I` belongs in LSF log filenames, not as an arbitrary command-argument substitution.

| Stage | Entries | Seeds | Levels | Revisions | Array elements | Single-call invocations |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Adapter smoke checks, inside build validation | 2 | 1 | 2 | 2 | None | 8 |
| Main quality comparison | 50 | 3 | 2 | 2 | **150** | **600** |

Use a fixed archived order within each element: `L2/baseline, L2/candidate, L3/baseline, L3/candidate`. Keep the existing single-call schedule; report its baseline-first order and lack of repeated processes as timing limitations. Same-host pairing does not eliminate order effects or host-load noise, and CPU models may differ between elements. Keep `RAYON_NUM_THREADS=1`, `QISKIT_NUM_PROCS=1`, `QISKIT_PARALLEL=FALSE`, and BLAS/OpenMP limits of one. Fix backend seed `424242`. The level-2/3 presets retain their 20 SABRE trials and layout iteration limits of 2/4. No GPU or IBM service credentials are required.

The concurrency cap is **global per campaign**. If a larger optional corpus exceeds `MAX_JOB_ARRAY_SIZE`, submit chunks serially or share one enforced cap across chunks. Multiple independent `%16` arrays must not silently exceed a requested total of 16.

## 4. Implementation work, in dependency order

### A. Profile, immutable inputs, and schemas

Create `tools/benchpress/profiles/{quality,followup-quality,full}.json`, `panel.py`, and `schemas.py`. Encode the fixed 50-entry panel from the comparison plan and resolve actual collected node IDs. Freeze selection before execution; resource-limit failures remain outcomes instead of causing automatic panel shrinkage.

The main profile defaults to seeds 0–2, both levels, and one invocation per revision/level/seed. A separately requested quality follow-up uses an explicit selected panel and seeds 0–9. Optional full defaults to eligible corpus entries, seeds 0–4, and one invocation per revision/level/seed. Full uses a 900-second child limit, a 3900-second internal element limit, `1:10` LSF wall time, and an explicitly supplied campaign budget. All profiles record single-call runtime; none performs calibration, warmups, repeated timing rounds, or process repetitions. `full` means expanded corpus coverage, not a calibrated full timing benchmark. A repeated, order-balanced benchmark would require a separately designed campaign.

Record the runtime measurement contract in new profiles/manifests: `runtime_method="perf_counter"` and `runtime_scope="benchmark_callable"`, measured in seconds. Keep archived untimed manifests readable and unchanged; the collector reports their timing as unavailable. Carry timing in existing per-invocation artifacts so array size, invocation identity, stage names, and recovery behavior remain compatible.

Reject duplicate/empty/unsupported level lists; initially support `2`, `3`, and `2 3`. Validate non-negative distinct seeds, positive resource limits, element/LSF deadline slack, and explicit hashes. Snapshot harness, Qiskit, Benchpress, adapter, dependencies, and selected inputs. Cache identity includes source SHA, ABI, Linux/glibc compatibility, Rust/build flags, and lock/adapter hashes; `2.6.0.dev0` alone is insufficient.

### B. Linux build and correctness validation

Implement `prepare.py`. Build the two release wheels in isolated source/target/wheel directories with Python 3.12, Rust 1.89, and identical build dependencies. Build sequentially, with `CARGO_BUILD_JOBS` bounded by the build allocation. Resolve dependencies once, install the same versions for both revisions, and run `pip check`. Create a separate locked analysis environment for reporting/plots.

Create environments at their final shared paths; workers never install packages or relocate environments. Capture toolchain versions, locks, wheel/extension hashes, and actual module import paths. Use Linux artifacts, not laptop macOS wheels. Prefetch dependencies/crates on a network-enabled host if compute nodes are offline. Publish reusable cache entries atomically with locking.

Run affected tests for each revision, collection/config checks, small semantic/reproducibility checks, and the eight single-call adapter smoke invocations (SU2-89 and `qaoa_n6`/linear, both levels/revisions). They validate implementation and do not estimate workload duration or change panel membership. Publish `build/ready.json` only after success. Submit the main array directly after the build job and this marker are confirmed successful.

### C. Single-call quality and runtime adapter

Implement `adapter.py` and an archived patch against the pinned Benchpress tree:

1. Add explicit `seed_transpiler` to every relevant preset-manager call, and a fixed seed to the generic backend constructor.
2. Resolve `BenchpressConfig` from absolute `BENCHPRESS_CONFIG` before imports. Generate immutable `configs/opt-2.conf` and `opt-3.conf`, differing only in optimization level, and assert the actual level matches each task.
3. Retain `BENCHPRESS_QUALITY_ONLY=1` and execution mode `quality` for compatibility. Run with **`--benchmark-disable --timeout-skip-list=0`**. The pinned fixture then invokes the callable once without calibration or repeated rounds. A nonzero timeout preflight would add another call and must be rejected, as must upstream profiling/repetition overrides. In `quality_plugin.py`, wrap the actual benchmark callable with `time.perf_counter()` immediately around its execution. This interval includes `pm.run(circuit)` and excludes loading, pass-manager construction, pytest startup, quality-metric extraction, and validation. Count exactly one call; timing must not add a second transpilation.
4. Preserve circuit loading, native output metrics, and gate/connectivity validation. In quality mode bypass QASM-loader `perf_counter()` calls and its `qasm_load_time` metadata.
5. Add a pytest reporting hook to capture `benchmark.extra_info` after the call and commit a **`quality.json`** record with identity, allowed input/output fields, final setup/call/teardown status, and top-level `runtime_s`, `runtime_method="perf_counter"`, and `runtime_scope="benchmark_callable"`. Handle a skip/failure before fixture creation with nullable metrics. Disabled benchmarks do not create normal statistics records, so a benchmark-JSON hook alone is insufficient.
6. Record single-call evidence and assert exactly one callable execution for successful tests. Require finite positive runtime for timing comparisons. Export partial failure metadata without treating it as a passing quality/runtime result. Sort collection and archive all identities. A failed callable or later validation/teardown failure must never contribute a successful runtime sample to aggregates.

The fixture behavior is documented by the pinned [source](https://github.com/1ucian0/pytest-benchmark/blob/e33f5934cb462e719b13b8d352ced107f575b4f7/src/pytest_benchmark/fixture.py). Keep the plugin installed for Benchpress fixture/hook compatibility, but request no upstream benchmark JSON, repeated-round statistics, calibration, warmup, or profiling output. Runtime comes from the adapter's single-call interval.

Every invocation has a **private working directory, pytest cache, temp directory, skipfile state, and output path**. Pass an absolute test node path, explicit `--rootdir`, and private `-o cache_dir=...`. Make only the frozen Benchpress source importable; never expose Qiskit source ahead of its installed wheel. Set `PYTHONDONTWRITEBYTECODE=1` and preserve shared sources read-only. Verify isolation with concurrent synthetic children.

Stage immutable HamLib data to task-local scratch if useful, keyed by hash. Persist outputs to shared storage after each child. Scratch may disappear on termination. Ordinary pytest/LSF logs may contain incidental durations; do not extract or compare them as compilation performance.

### D. Element executor and context

Implement `run.py` and `lsf/job.py`. Validate `LSB_JOBID`, `LSB_JOBINDEX`, the manifest hash, one-host allocation, platform compatibility, exclusive-core reservation, and Linux affinity. Select one hardware thread from the allocated physical core and record its SMT sibling mapping. Capture actual CPU metadata without requiring a model selector. Keep wrapper/watchdog activity lightweight.

Run the four children sequentially using absolute environment Python paths and the single-call options. Use `--junitxml` for test diagnostics and the adapter's `BENCHPRESS_QUALITY_OUTPUT` absolute path for quality/runtime JSON. Enforce a **300-second whole-child timeout** with a process-group watchdog. A failed/timed-out revision must not suppress its counterpart; continue the remaining children when deadlines permit. The watchdog interval is not the compilation-runtime metric, and no scheduler changes or additional invocations are needed to capture timing.

Write initial `run.json` before starting each child and atomically finalize it afterward. Commit an element outcome only after all planned children have statuses. Trap termination and clean up descendants. Enforce a 25-minute internal deadline within the 30-minute LSF limit, leaving time to flush output. If the worker dies before writing final artifacts, the collector synthesizes missing/terminated outcomes using the schedule and scheduler state. Distinguish timeout, OOM, test failure, precondition failure, cancellation, and not run.

### E. Launcher, manager, and recovery

Create `tools/benchpress/lsf/{submit,manager,scheduler,context,ledger,control,job}.py` and `__init__.py`. Keep launcher/control/manager dependencies in Python 3.12's standard library; use the locked analysis environment for reports. Require Linux LSF clients with the `bjobs -json` fields used by the adapter. Adapt reference patterns without importing its `qtb` package.

```text
launcher -> manager -> build/validation + freeze panel
        -> quality array -> aggregate/report -> terminal summary
```

Use one manager lock and append-only submission events. Persist intent, unique nonce-bearing name, owner, stage, indices, and resources before `bsub`. Reconcile ambiguous responses by identity rather than resubmitting blindly. `PEND` remains pending. A missing `bjobs` row requires ledger/outcome/`bhist` reconciliation, not an assumption of success.

Start the **180-minute campaign deadline when the first quality worker starts**, using an atomic shared marker. It includes later queue waits. At the deadline, cancel unfinished benchmark jobs owned by this campaign, confirm terminal states, and allow reporting under its own wall limit. Build/validation and initial queue wait have separate limits. Workers check the shared deadline before starting children; resource clocks are job controls, not experimental measurements. Failed scheduler queries leave termination unresolved.

Submit reporting after all owned array elements settle, including failures. Prefer manager-side reconciliation; a success-only `done` dependency must not leave reporting waiting forever after an element fails.

There are no automatic retries. Resume may rerun incomplete elements only after old jobs are confirmed terminal. Rerun all four children in a new attempt on one allocation; preserve the original outcomes and never combine results from different attempts. Fix any canonical retry-selection rule before reviewing quality values. Recovery starts a new explicitly recorded campaign-budget epoch.

### F. Collector and reports

Implement `compare.py` and `lsf/report.py` as the sole writers of aggregate artifacts. Reconcile all scheduled results; reject mismatched source/input/config/level/seed/host/allocation/attempt identities. Extract gate count, two-qubit depth, operation histogram, input/output width, validation status, and the adapter's runtime fields from `quality.json`. Accept timing only when `runtime_s` is a finite positive number and timer/scope match the supported contract. Reject boolean, zero, negative, non-finite, and nonnumeric timing values. Do not require upstream benchmark statistics or infer runtime from process, JUnit, or LSF durations.

Pair revisions within each level/seed, then summarize seed quality changes per case. Report separate level-2 and level-3 results, absolute/percentage differences, zero-baseline quality categories, wins/ties/losses, quality regressions, and coverage. For runtime, export baseline/candidate seconds, `runtime_change_s = candidate - baseline`, `runtime_ratio = candidate / baseline`, and `runtime_change_pct = 100 * (runtime_ratio - 1)`. Positive change means slower; a ratio below one means faster.

Track `runtime_status` independently of quality: `available/missing/invalid/not_applicable` on invocation rows, `paired/missing/invalid/ineligible` on seed pairs, and `complete/incomplete` on cases. Preserve quality comparisons for historical artifacts with missing timing and for otherwise valid artifacts with invalid timing. A runtime-complete case requires all configured seeds to have valid quality and timing pairs. Aggregate per-revision median seconds and medians of paired per-seed changes/ratios/percentages; do not subtract independent medians. Include runtime coverage, median changes, geometric-mean ratios, and faster/tie/slower counts per level, suite, and topology in `groups.csv` and `summary.json`. Add `runtime_regressions.csv` for complete cases with runtime increases while retaining `regressions.csv` for quality regressions.

Cross-level rows compare L3/L2 within each revision and common complete panel. Runtime fields require complete runtime pairs at both levels; historical rows retain quality with blank runtime. Preserve first-attempt and retry history separately. Include per-level runtime scatter/topology plots and cross-level runtime scatter plots alongside quality plots. State the single-call timing limitations and separate quality/runtime denominators in scientific and operational reports. Report observed quality/runtime tradeoffs without a calibrated speedup claim or campaign ETA.

## 5. Submission command and arguments

After implementation is committed on the current branch, check it out on the cluster's shared filesystem. Replace `/shared/PROJECT/USER/...` with the actual mount. The supplied reference did not specify this repository's shared location, module setup, or accounting project.

```bash
# Cluster login node, after cloning/updating this branch:
BP_REPO=/shared/PROJECT/USER/qiskit-evolved
BP_RESULTS=/shared/PROJECT/USER/benchpress-results/sabre-quality-o2-o3-20261014-01
cd "$BP_REPO"
git switch sabre-choose-best-swap-heuristics-benchpress

# Load the site's Python 3.12 module first if needed.
python3.12 -m venv "$BP_REPO/.benchpress-control"
BP_PY="$BP_REPO/.benchpress-control/bin/python"
mkdir -p /shared/PROJECT/USER/benchpress-results
command -v bsub bjobs bhist bkill git rustup cc

"$BP_PY" tools/benchpress/lsf/submit.py \
  --repo "$BP_REPO" \
  --harness-ref "$(git rev-parse HEAD)" \
  --baseline-ref 1298f960b7e814cfcb6aa9338b548ec5c6d2b355 \
  --candidate-ref 90ccaac29b2a547592e870797265007a128a02eb \
  --results-root "$BP_RESULTS" \
  --profile quality \
  --opt-levels 2 3 \
  --panel-size 50 --seeds 0 1 2 \
  --max-parallel 16 \
  --queue normal --lsf-memory-unit GB \
  --task-mem-gb 8 --task-wall 0:30 \
  --run-timeout-s 300 --campaign-limit-min 180 \
  --build-slots 16 --build-mem-gb 64 --build-wall 6:00 \
  --manager-wall 24:00 \
  --dry-run
```

`--dry-run` validates inputs and prints the effective matrix/resources and initial `bsub` arguments without submitting or occupying the final session path. Remove **only `--dry-run`** to submit the campaign. The launcher snapshots code, creates `<results-root>.lsf/`, submits one manager, and prints job IDs, paths, and status/stop commands. Do not separately submit individual stages for that session.

| Argument | Default / example | Meaning |
| --- | --- | --- |
| `--repo PATH` | Required | Shared Git checkout with both Qiskit source pins and harness code. |
| `--harness-ref REF` | `HEAD`, resolved once | Committed implementation to snapshot. |
| `--baseline-ref`, `--candidate-ref` | Explicit hashes above | Algorithm source identities independent of harness history. |
| `--results-root PATH` | Required | New absolute shared session directory; scheduler state lives beside it in `PATH.lsf`. |
| `--profile` | `quality` | `quality`, `followup-quality`, or `full`; each measures quality and runtime in single-call mode. |
| `--opt-levels` | `2 3` | Level list; optionally narrow to `2` or `3`. |
| `--panel-size` | `50` for quality | Entries before level expansion; full defaults to the eligible corpus. |
| `--panel-file PATH` | Omitted; required for focused follow-ups | Frozen selected panel with identities and reasons; snapshot/hash it. |
| `--seeds` | `0 1 2` for quality | Exact seed list. Follow-up defaults 0–9; full defaults 0–4. |
| `--backend-seed` | `424242` | Fixed generic backend properties. |
| `--max-parallel` | `16` | Global cap on running array elements; `32` is optional if resources allow. |
| `--queue` | Site default if omitted; example `normal` | Fallback queue; role overrides are `--build-queue`, `--task-queue`, `--manager-queue`, `--report-queue`. |
| `--project` | Omitted | Optional LSF project passed as `-P`. |
| `--lsf-memory-unit` | `GB` at referenced site | Convert GB requests to configured LSF units; support `MB` elsewhere and record conversion. |
| `--task-mem-gb`, `--task-wall` | `8`, `0:30` | Worker reservation and execution limit. |
| `--run-timeout-s` | `300` | External whole-child watchdog; no plugin timeout preflight. |
| `--element-timeout-s` | `1500` | Internal element limit, leaving termination/output slack before LSF wall time. |
| `--campaign-limit-min` | `180`; explicit for full | Administrative limit starting with the first quality worker; includes later queue waits. |
| `--build-slots`, `--build-mem-gb`, `--build-wall` | `16`, `64`, `6:00` | Build/validation resources. |
| `--manager-wall` | `24:00` | Manager execution limit; one slot / 4 GB. |
| `--report-wall` | `0:30` | Reporter execution limit; one slot / 4 GB. |
| `--job-env-script PATH` | Omitted | Shared module/compiler setup script; snapshot/hash and source it in job wrappers. |
| `--scratch-root PATH` | Job-local temp if available | Temporary staging; durable results always go to shared output. |
| `--dry-run` | Off | Print the planned submission only. |
| `--resume` | Off | Recover a session using archived identities/settings after all old jobs are terminal. |
| `--log-level` | `INFO` | Readable/JSONL orchestration logs; `DEBUG` retains scheduler diagnostics. |

Explicit arguments override profile defaults. Reject upstream benchmark calibration/repetition options; adapter runtime measurement is automatic for new invocations and needs no additional CLI flag. Changes to source identities, levels, seeds, panel, timeouts, or dependency locks require a new session. Both the results directory and its `.lsf` sibling must be new unless resuming. Increasing concurrency to 32 reserves up to 32 physical cores / 256 GB across hosts, while each invocation remains single-threaded. Collecting timing for an older untimed campaign requires a new session using the updated committed harness; re-reporting or resuming its archived harness cannot recover a missing measurement.

## 6. Generated array submission

The manager generates the equivalent of this example for the main panel. Use the launcher above, not a second manual submission into the same session.

```bash
bsub \
  -J 'bp-RUNID-quality[1-150]%16' \
  -q normal -n 1 -W 0:30 \
  -R 'span[hosts=1] affinity[core(1,exclusive=(core,alljobs))] rusage[mem=8]' \
  -o "$BP_RESULTS.lsf/logs/jobs/quality.%J.%I.out" \
  -e "$BP_RESULTS.lsf/logs/jobs/quality.%J.%I.err" \
  /bin/bash "$BP_RESULTS.lsf/scripts/quality-worker.sh"
```

Create log directories before submission. The wrapper sources the optional environment script and executes archived `lsf/job.py` with the absolute root, stage, and manifest hash. The worker resolves `LSB_JOBINDEX`. `%J/%I` produce unique job/element log filenames. [IBM array I/O documentation](https://www.ibm.com/docs/en/SSWRJV_10.1.0/pdf/spectrum-lsf-10.1.0-documentation.pdf).

Build, manager, and report are individual jobs. Optional `--benchpress-source` and `--benchmark-source` Git mirrors support offline build nodes; `--build-cache-root` enables a locked, verified release-wheel cache. See the [usage guide](../tools/benchpress/README.md) for these options and offline dependency setup. Workers are not automatically rerunnable. Manager restart requires tested locking and ledger reconciliation; explicit retries preserve attempt identities.

## 7. Output locations and schemas

For the command above, scientific artifacts live under `/shared/PROJECT/USER/benchpress-results/sabre-quality-o2-o3-20261014-01/` and scheduler state beside it under the `.lsf/` sibling. Start with **`<results-root>/results/report.md`**.

```text
<results-root>/
  harness/
  src/{baseline,candidate,benchpress}/
  env/{baseline,candidate,analysis}/
  wheels/{baseline,candidate}/
  build/ready.json
  tasks/quality.jsonl
  failed-builds/                        # Preserved interrupted builds after explicit resume
  results/
    inventory.jsonl
    manifest.json
    profile.json
    panel.jsonl
    cases.jsonl
    configs/{opt-2,opt-3}.conf
    benchpress-reproducibility.patch
    validation/
    raw/<stage>/<panel-id>/opt-<level>/seed-<s>/attempt-<a>/<revision>/
      run.json
      quality.json
      junit.xml
      stdout.log
      stderr.log
    tables/{runs,paired_seeds,cases,coverage,regressions,runtime_regressions,levels,groups,retry_runs}.csv
    summary.json
    report.md
    plots/opt-2/*.{svg,png}
    plots/opt-3/*.{svg,png}
    plots/levels/*.{svg,png}
    checksums.sha256
<results-root>.lsf/
  launch.json
  ledger.json
  scripts/                              # Exact wrappers and bsub arguments
  outcomes/                             # Worker/scheduler statuses and reconciled/ element outcomes
  logs/manager-<attempt>.log
  logs/events.jsonl
  logs/jobs/<stage>.%J.%I.{out,err}
  diagnostics/                          # Scheduler/allocation evidence
  report.md                             # Operational status and recovery summary
```

`run.json` holds invocation identity/provenance, host/affinity/job/index, command/environment, exit status/reason, and output paths. UTC timestamps serve recovery/deadline handling. `quality.json` holds schema version, execution mode, call count, validation status, input/output widths, native two-qubit gate count/depth, operation histogram, and top-level callable runtime seconds/timer/scope. `runs.csv` exports these timing fields and independent runtime status. Unavailable timing uses JSON `null` and empty CSV cells. Older records without timing remain readable for quality comparisons. The comparison plan defines CSV columns and independent completeness rules.

Write records by atomic rename on the same shared filesystem. Workers never append to shared CSV/JSON files. Preserve missing/failed/unstarted rows. For the main first attempt, expect **150 element outcomes, 600 invocation rows, 300 paired-seed rows, and 100 level-specific case rows**. Smoke validation is separate; retries preserve the original denominator and add attempt records.

Keep raw quality/runtime JSON, JUnit/logs, manifests, wheels, and source/input identities. Plots show baseline/candidate quality and eligible runtime, topology effects, and level differences. When all timing is unavailable, retain quality plots and explain the missing runtime coverage. Save final artifacts on shared storage even when scratch is cleaned. LSF jobs do not modify/commit the development checkout; publishing a result summary in `design/` is a later explicit step.

## 8. Monitoring, stopping, and resuming

Use these implemented commands:

```bash
"$BP_PY" tools/benchpress/lsf/control.py status --results-root "$BP_RESULTS"
tail -f "$BP_RESULTS.lsf/logs/manager-1.log"

# Replace the numeric example with the actual array ID:
bjobs -A 812345
bjobs -l '812345[7]'
bhist -l '812345[7]'

"$BP_PY" tools/benchpress/lsf/control.py stop --results-root "$BP_RESULTS"
"$BP_PY" tools/benchpress/lsf/control.py reap --results-root "$BP_RESULTS"
"$BP_PY" tools/benchpress/lsf/submit.py --resume --results-root "$BP_RESULTS"
```

`stop` cancels only this session's owned jobs and confirms termination; `reap` handles orphaned jobs; `resume` continues incomplete work using the same archived configuration and a new attempt/manager epoch. Check job ID, unique name, and owner before cancellation. Never use `bkill 0` or broad matching that can affect unrelated jobs. Refuse resume while any previous manager/worker is active or unresolved.

Status distinguishes pending, running, terminal success/failure, unresolved scheduler state, and report completion. The campaign finishes only when jobs are terminal and every scheduled invocation has an outcome. Show counts and states without extrapolating a runtime forecast. [IBM array status/history](https://www.ibm.com/docs/en/spectrum-lsf/10.1.0?topic=information-viewing-job-array).

## 9. Implementation validation

Use local fake-scheduler tests patterned on the reference, without requiring LSF:

- Source pins stay fixed as harness `HEAD` advances; planning commits never become the baseline.
- Matrix expansion gives 150 elements / 600 invocations / 300 pairs / 100 cases, with unique one-based indices.
- The manager submits build/validation, one quality array, then reporting; it never performs a preliminary workload-sizing run.
- Each successful child calls the transpilation callable exactly once with upstream calibration/repetition and timeout preflight disabled. The adapter measures only that call with a monotonic timer and exports positive finite runtime, seconds units, and method/scope metadata. Quality export works without upstream benchmark statistics, and timed QASM-load metadata is bypassed.
- Both levels reach the actual preset manager and quality metadata. Qiskit imports come from the intended wheels.
- Concurrent synthetic children isolate configs, skipfile state, caches, and output. Gate/depth/runtime fields and failure records survive the reporting hook. Verify timing excludes setup, loading, validation, and pytest teardown without adding callable executions.
- Allocation checks, platform compatibility, unit conversion, fixed threads, and the global array cap work without CPU-count/model/load selectors.
- Ambiguous submission, failed queries, pending/history expiry, worker SIGKILL, deadlines, and failed-element reporting produce no duplicate jobs or assumed success.
- Resume preserves original failures and cannot combine different attempts. Missing/zero quality metrics and incomplete pairs are represented correctly.
- Runtime seconds differences, candidate/baseline ratios, percentage direction, paired-seed median aggregation, grouped denominators, cross-level completeness, and separate regression tables are verified with deterministic samples. Runtime plots and report language include seconds and single-call limitations.
- Historical untimed records still produce quality tables/plots. Missing, zero, negative, non-finite, nonnumeric, wrong-scope, and failed-invocation timing cannot enter runtime aggregates; invalid timing alone does not discard otherwise valid quality results.

Small live-cluster plumbing checks may be used during implementation, but no separate acceptance campaign is a prerequisite for the user's main run. Submit the single quality campaign after implementation checks pass. Its build validation precedes the main array automatically. No quality conclusion is supported until these jobs execute. Local tests exercise the real pinned benchmark fixture, synthetic worker failures, fake scheduler reconciliation, cache integrity, and report completeness. The optional upstream integration test resolves the full inventory and executes SU2-89 and `qaoa_n6`/linear at both levels with an installed Qiskit package; it does not replace Linux validation of the two algorithm revisions.
