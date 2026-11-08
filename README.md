# nix-ninja-incremental

Validated persistent dependency discovery for [PDT Partners' nix-ninja](https://github.com/pdtpartners/nix-ninja), with independent artifact tests and reproducible planning/build measurements. This is an independent extension against a pinned upstream revision.

The frozen reuse implementation has passed native artifact/recovery checks, the full 256-seed edit campaign, real-workload audits and full Nix functional checks. Repeated planning and store-warm evaluation are complete; corrected cold-build evaluation is in progress. Broad-header regressions remain visible in the study.

## What it does

Local planning can retain a dependency-discovery result and reuse it after validating the current action identity and filesystem observations. Those observations include file contents, negative header lookups, search order, symlink resolution and file kinds. An unchanged timestamp is insufficient.

Three modes make the behavior inspectable:

| Mode | Behavior |
| --- | --- |
| `off` | Fresh discovery without retained metadata |
| `record` | Fresh discovery, with persisted observations and candidate auditing |
| `reuse` | Validated hits can avoid supported discovery work; misses discover again |

Graph parsing, input materialization, derivation generation and realization still use current inputs. This caches discovery metadata; Nix continues to handle build outputs. Generated inputs and unsupported preprocessing patterns visibly bypass reuse. Each invocation emits an `NNI_DISCOVERY=` JSON report with hits, scans, validation/fallback reasons and observation costs.

## Execution contract

Reuse requires a private controller-owned input view with exclusive source ownership. The Python controller holds its lease during edits; the Rust CLI holds it during planning/build execution. Cache state lives outside the inputs and is disposable, versioned, checked and bounded. Cache-lock contention can use fresh uncached discovery; input-view contention is rejected.

The source guard supports a bounded set of literal dependencies and object-alias queries. It is not a general C++ preprocessor. Stable source/tool/store inputs during an invocation are part of the contract. Ordinary live editor workspaces and concurrent store collection are outside that contract.

## Reproduce the integration checks

Use native x86_64 Linux, Docker, Git and Python 3.12+. The pinned environment uses four CPUs, 6 GiB memory and 2,048 PIDs. It creates a project-labelled container with its own Nix store and a workspace bind mount. The runtime enables the experimental Nix features required by the pinned upstream; initial setup builds the pinned runtime and tools.

```sh
python3 tools/variants.py prepare recording
python3 tools/dev.py up
python3 tools/dev.py runtime
python3 tools/dev.py info
python3 tools/dev.py exec -- nix-shell --impure \
  --expr '(import /workspace/environment/baseline.nix { source = /workspace/.work/nix-ninja-recording; cliRuntime = true; }).shell' \
  --run 'python3 /workspace/tests/recording_checks.py && python3 /workspace/tests/lifecycle_checks.py'
python3 tools/dev.py stop
```

The artifact oracle rebuilds each reference state with fresh independent Ninja, compares artifact bytes and program behavior, checks all-mode derivation agreement, and injects skipped-rebuild faults. Fixtures include preserved mtimes, missing/shadowing headers, aliases, embedded resources, invalid source/recovery, generated producers and graph transitions. Lifecycle checks cover malformed state, publication/recovery, permissions and locks.

Raw logs, manifests and results stay under ignored `results/`; pinned source/build trees stay under ignored `.work/`. `stop` retains the project container's store for inspection. Native benchmark workflows use fresh runners; cold trials require a fresh disposable project store and exact restoration of valid paths and pinned CA build-trace maps between modes. Darwin-hosted emulated runs are diagnostic, not final timing evidence.

For a focused demonstration after setup, run the ten-state graph oracle:

```sh
python3 tools/dev.py up
python3 tools/dev.py exec -- nix-shell --impure \
  --expr '(import /workspace/environment/baseline.nix { source = /workspace/.work/nix-ninja-recording; cliRuntime = true; }).shell' \
  --run 'python3 /workspace/tools/build_oracle.py --fixture /workspace/tests/fixtures/graph_build_edits.json --discovery-mode reuse'
python3 tools/dev.py stop
```

It checks changed generators, generated outputs, implicit/order-only edges, output renaming and command changes against fresh independent Ninja results. Stable actions reuse while generated consumers retain conservative fallback. The printed result directory contains per-state artifacts, discovery counters and warm plans. This is a correctness demonstration.

## Native workflows and measurements

The workflows use pinned actions, source revisions, tools and container/runtime definitions:

| Workflow | Purpose |
| --- | --- |
| [correctness](.github/workflows/checks.yml) | Rust formatting/lints/tests, Python oracles and CLI contracts |
| [recorded planning](.github/workflows/recording-state.yml) | Independent off/record/reuse artifacts, lifecycle and owned-store collection |
| [full Nix workload](.github/workflows/nix-workload.yml) | Actual Nix target realization and functional probes |
| [large campaign](.github/workflows/large-campaign.yml) | 256 seeds × 32 transitions across all three modes |
| [paired planning](.github/workflows/planning-evaluation.yml) | Ten first-post-edit pairs per declared planning scenario and workload |
| [paired builds](.github/workflows/build-evaluation.yml) | Store-warm end-to-end pairs and separately sharded cold/full pairs |
| [contextual baselines](.github/workflows/baseline-evaluation.yml) | Unmodified/corrected compatibility and paired no-op overhead |
| [planner API counts](.github/workflows/operation-counts.yml) | Matched frozen/instrumented plans and diagnostic operation counts |
| [cold debug reproduction](.github/workflows/debug-repro.yml) | Legacy debug-path mismatch and mapped byte equality with debug information retained |

The final protocol is in [benchmarks/workloads.json](benchmarks/workloads.json): pinned Nix and Zstd, no-op/irrelevant/implementation/shared-header edits and cold builds, seed 2027. Real-workload content probes are nonsemantic; deterministic fixtures separately test semantic invalidation. Planning and realization are measured separately. Setup, metadata warming, resets, publication and cleanup remain visible; compiler-result caching is disabled and OS page caches are uncontrolled.

[campaign_summary.py](tools/campaign_summary.py) requires exact seed/mode/state coverage and checks raw source, artifact, plan and provenance records. [evaluation_summary.py](tools/evaluation_summary.py) requires exact paired coverage and validates raw audits/plans, input edits, functional probes and store states. It reports absolute times, median paired differences/ratios and fixed-seed bootstrap intervals, retaining failed/incomplete results. Child RSS excludes independent daemon workers; cgroup boundary/cumulative values are separately labelled context.

[baseline_summary.py](tools/baseline_summary.py) independently checks contextual compatibility, raw plans/audits, binary labels and the exact seeded measurement order. It recomputes each matched baseline comparison separately for current-off and warm reuse, preserving failed or different-plan probes as exclusions. Analyze each calibration separately; duplicate workload reports are rejected.

After downloading a contextual-baseline workflow's artifacts into `results/baseline-calibration`, verify them with the frozen release identity:

```sh
python3 tools/baseline_summary.py results/baseline-calibration \
  --expected-binary-sha256 a9bdf2d1ba5d26a136e1b64602d9e0cc2b56232038e47ce006d3c30bc164052a \
  --output results/baseline-summary.json
```

The API-count diagnostic keeps the frozen helper/tool paths while invoking a separately instrumented binary. It verifies matching source edits, discovery audits and raw plans before interpreting profiles. Counts are planner API attempts, including failures, rather than unique objects or daemon work. Worker durations overlap; diagnostic elapsed times stay separate from the frozen performance samples.

After downloading both operation-count artifacts into `results/operation-counts`, verify all eight workload/scenario comparisons:

```sh
python3 tools/operation_summary.py results/operation-counts \
  --output results/setup/verified-operations.json
```

Cold trials using the debug-prefix helper select `debug_paths=mapped` in the build workflow and `--cold-control mapped-debug-paths-v1` during analysis. Legacy and mapped data are separate epochs. The tiny debug reproduction passes; full mapped cold pilots remain in progress.

## Evidence and scope

The frozen reuse binary has passed [native artifact/lifecycle checks](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42431292377), the [full 256-seed campaign](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42433715029), [expanded graph and recovery checks](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42438687034), [real workload audits](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42431292426), and [full Nix functional validation](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42432062401). These provide bounded evidence for the stated contract. Whole Nix functional validation is distinct from independent whole-binary equivalence; the small fixtures compare fresh bytes directly.

Exact upstream/workload/runtime pins are in [environment/pins.json](environment/pins.json). [patches/series.json](patches/series.json) records patch hashes, dependencies and expected source postimages; `tools/variants.py` verifies them before preparing a variant. Profiling and historical baseline variants are separate from the final `recording` stack.

Trace validation and content-addressed build ideas are established prior work, including [Build Systems à la Carte](https://www.microsoft.com/en-us/research/publication/build-systems-la-carte/). The contribution here is the bounded nix-ninja integration, correctness work and auditable evaluation. No algorithmic novelty, upstream adoption or production-use claim is made.
