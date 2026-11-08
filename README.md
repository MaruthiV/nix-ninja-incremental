# nix-ninja-incremental

Validated persistent dependency discovery for [PDT Partners' nix-ninja](https://github.com/pdtpartners/nix-ninja), with independent artifact tests and reproducible planning/build measurements. This is an independent extension against a pinned upstream revision.

The frozen reuse implementation has passed native artifact/recovery checks, the full 256-seed edit campaign, real-workload audits and full Nix functional checks. Planning, store-warm and mapped cold evaluation are complete: 180 pairs / 360 timed calls. Six of eight predeclared performance targets pass; both store-warm shared-header targets fail. The results support opt-in use within the contract below.

## Measured results

Each primary case has ten native off/reuse pairs. Positive improvement means reuse is faster; effects are medians of per-pair `1 - reuse/off`, with fixed-seed paired-bootstrap 95% intervals.

| Scenario | Nix improvement [95% interval] | Zstd improvement [95% interval] |
| --- | ---: | ---: |
| No-op planning | +23.23% [+21.74, +23.75] | +31.05% [+29.55, +35.87] |
| Implementation-edit planning | +22.98% [+21.99, +23.93] | +21.13% [+19.59, +27.75] |
| Shared-header store-warm build | -58.37% [-58.95, -56.34] | -43.79% [-46.02, -39.34] |
| Cold build | -0.406% [-1.556, +0.404] | -0.084% [-0.633, +0.317] |

The four no-op/implementation planning targets exceed 20% at the median; both cold cases meet the 5% median-regression limit. Both shared-header cases miss that limit. Cold intervals include zero, so no cold speedup is demonstrated. Store-warm builds prewarm the declared state union; they do not measure first recompilation.

A separate [matched three-way Zstd no-op control](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42444066454) finds a modest **7.13% net planning benefit [2.71%, 10.57%]** over the simpler corrected baseline, including the feature's overhead. Nix contextual plans differ, so no equivalent-work net Nix gain is claimed. These results do not justify default enablement or universal acceleration.

Raw evidence and machine manifests are retained in the [planning](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42437597957), [store-warm](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42438070314), [cold pilot](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42457149027) and [remaining cold](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42463748456) workflow artifacts, subject to GitHub retention. Download the successful Nix 5 retry artifact by ID when both attempts share a name. The original dependency-download failure is preserved; no successful measured pair was rerun.

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
| [cold debug reproduction](.github/workflows/debug-repro.yml) | Legacy/mapped debug bytes and recovery after collection with traces and metadata retained |

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

Cold trials using the debug-prefix helper select `debug_paths=mapped` in the build workflow and `--cold-control mapped-debug-paths-v1` during analysis. Legacy and mapped data are separate epochs. All twenty mapped pairs pass strict saved artifact/object bytes, plans/probes, distinct native preflights and exact store resets; complete within-pair NAR hash/size/reference and CA trace maps also match. No stripping or hash normalization is used. The original failed setup and successful same-commit Nix 5 retry remain separate retained records.

Extract successful pilot and remaining-pair artifacts into one directory with distinct subdirectories. Include only successful mapped reports: ten per workload, indices 0–9. Verify their complete primary coverage with:

```sh
python3 tools/evaluation_summary.py results/cold-mapped \
  --kind cold --rounds 10 --cold-control mapped-debug-paths-v1 \
  --expected-binary-sha256 a9bdf2d1ba5d26a136e1b64602d9e0cc2b56232038e47ce006d3c30bc164052a \
  --output results/cold-summary.json
```

The cold study keeps compiler flags/debug information, requires zero initial discovery hits, and restores protected paths and CA trace maps between modes. Source capture, preparation, resets, verification copies and post-build plans remain separate from timed CLI calls. The measured call includes validation, observation recording and metadata publication. CLI/waited-child RSS excludes independent daemon workers; no complete controller-wall-time or total-memory claim is made.

For debug-path mapping, put the opt-in [task helper](benchmarks/cold-debug/control.nix) ahead of the legacy helper on the controller's task `PATH`. It preserves debug information. After the pinned container/runtime setup, reproduce the [native collection recovery check](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42463766517):

```sh
python3 tools/dev.py exec -- nix-shell --impure \
  --expr '(import /workspace/environment/baseline.nix { source = /workspace/.work/nix-ninja-recording; cliRuntime = true; }).nixWorkload.shell' \
  --run 'python3 /workspace/tests/debug_collection_checks.py --disposable-store daemon'
```

The runner selects that helper explicitly, builds a tiny `-g` target, collects its fixture paths in the disposable project store while retaining discovery metadata and CA traces, and rebuilds. It requires a real reuse hit, identical object/executable bytes and store paths, matching plans/probes and exact final store restoration. This is a bounded recovery check, separate from performance measurements.

## Evidence and scope

The frozen reuse binary has passed [native artifact/lifecycle checks](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42431292377), the [full 256-seed campaign](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42433715029), [expanded graph and recovery checks](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42438687034), [real workload audits](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42431292426), and [full Nix functional validation](https://github.com/MaruthiV/nix-ninja-incremental/actions/runs/42432062401). These provide bounded evidence for the stated contract. Whole Nix functional validation is distinct from independent whole-binary equivalence; the small fixtures compare fresh bytes directly.

Exact upstream/workload/runtime pins are in [environment/pins.json](environment/pins.json). [patches/series.json](patches/series.json) records patch hashes, dependencies and expected source postimages; `tools/variants.py` verifies them before preparing a variant. Profiling and historical baseline variants are separate from the final `recording` stack.

Trace validation and content-addressed build ideas are established prior work, including [Build Systems à la Carte](https://www.microsoft.com/en-us/research/publication/build-systems-la-carte/). The contribution here is the bounded nix-ninja integration, correctness work and auditable evaluation. No algorithmic novelty, upstream adoption or production-use claim is made.
