# Code and data separation in the tuner

**Goal:** the code says how to measure, decide and stay safe. Data, shipped or learned, says what is known about
settings, mechanisms and workloads. Data can then improve without a code release (hosted: `learning.db`), and the
code stays smaller and testable.

There are two kinds of data:
- **Shipped knowledge:** versioned files in the OSS repository (`agent/tuning_agent/knowledge/*.json`), reviewed like
  code. They are the same for every installation.
- **Learned data:** `learning.db`, built from observations by the learner. It is empty on a new installation and is the
  shared intelligence on the hosted service. It refines the shipped knowledge, and every entry carries its scope and
  repeatability (`docs/design/agentic-saas.md`).

## Inventory: what is hard-coded today (agent, 2026-10-20)

| What | Where | Size | Code or data? | Learnable from observations? |
|---|---|---|---|---|
| Variable catalog: kind, dynamic, risk, category, descriptions | `knowledge.KB` | 159 vars | data | partly: defaults and ranges per server version from the agents' own `SHOW VARIABLES` |
| Trade-off definitions: what a value gives up | `knowledge.TRADEOFFS` | 14 | data (policy) | no: a decision for people |
| Mechanism map: change → bottleneck components it relieves | `mechanisms.MECHANISMS` | 91 | data | **yes**: hypothesis outcomes show which components really moved |
| Variable preconditions (writes, reads, DNS, counters) | `mechanisms.VAR_NEEDS`, `VAR_COUNTERS` | 51 | data | partly: a counter that never moved before a change helped would show a precondition is wrong |
| CPU-profile categories (frame regex → category) | `cpuprof.CATEGORIES`, `bottleneck.CPU_PARTS` | 14 / 15 | data | **yes**: frames left in "other" can be classified by the LLM and confirmed across sessions |
| Off-CPU wait categories (frame regex → wait kind) | `probes.OFFCPU_CATEGORIES` | 16 | data | **yes**, as above |
| Rule candidates: when to propose a change, which values | `engine.recommend` | 94 rules, ~134 numeric cut-offs | mixed | the trigger is code; the **values and value grids** (sweep points, step sizes) are learnable: which values won in similar conditions |
| Diagnosis cut-offs (`cpu_busy ≥ 85`, `wait_free ≥ 1`, ...) | `engine.diagnose` | ~40 | data | partly. Since the evidence gate, diagnoses mostly name a problem; measured time decides relevance |
| Baseline formula constants (OS reserve, per-connection memory, 75% cap, redo / io_capacity sizing) | `baseline.generate` | ~15 | data | **yes**: measured mysqld memory beyond the buffer pool per version and connection count; redo and io_capacity outcomes per storage class |
| Noise defaults (minimum threshold, k) | `evaluate` | few | data | yes, already per environment (noise history); could be shared per environment class |
| Best-practice bundle membership | `engine.PRACTICE_IDS` | 9 | data | yes: bundle and bisect outcomes per setting |
| LLM prompts and schemas | `llmassist`, server `llm.py`, `learner.py` | 4 | data (text) | no: reviewed text |
| Evidence gate, Amdahl check, verdicts, confirmation, safety (trade-off policy, memory limits, rollback) | engine, evaluate, cli | – | **code** | no: these are the method and the guardrails |

## What should be data, ranked by value and practicality

1. **Mechanism effects (learned).** For each change and component: how often the targeted component shrank, and how
   often the goal moved. The hypothesis track record has this today; the next step is to use it as data in
   `relevance()`:
   - a change that "moved" its component five times without helping the goal ranks lower for that component;
   - a change that helped through a component it is not tagged with gains that tag, as an observed effect with
     scope.

   Code keeps the shipped tags as the starting map.
2. **Value grids (learned).** Sweep points and step sizes per variable and conditions: which values won, lost, or were
   skipped as worse, by hardware class, storage and workload. An agent sweeping `innodb_io_capacity` on a SATA SSD
   starts near the values that won on similar storage, plus one exploratory point. Code keeps the fallback grids.
3. **Profile classification (learned plus shipped).** Move `CATEGORIES` / `OFFCPU_CATEGORIES` / `CPU_PARTS` into
   `knowledge/profile_categories.json`. The learner proposes classifications for frames that dominate "other"
   (LLM-classified, then confirmed when the same frames recur across sessions), and agents load the shipped file plus
   the learned additions.
4. **Baseline constants (learned).** Per server version, measured mysqld memory beyond the buffer pool (fixed plus per
   connection), from runs (PSS + swap − buffer pool). The baseline uses the learned figure for the class instead of
   400 MB + 0.67 MB per connection, which was 6x too low on the AMD box.
5. **Shipped knowledge as files.** Move the static tables to `knowledge/*.json`:
   - `KB`, `TRADEOFFS`, `MECHANISMS`, `VAR_NEEDS` / `VAR_COUNTERS`, `PRACTICE_IDS`;
   - rule metadata: id, title, mechanism text, default value grid.

   Code reads them, and `learning.db` adds and adjusts per conditions. No behaviour change by itself; it makes 1-4
   possible without editing Python.
6. **Rule triggers (later, partial).** With the evidence gate in place, many rule triggers could become data:
   "propose change X when component C ≥ its noise share and precondition P holds". That is a declarative rule table
   (component, precondition, change, value grid) evaluated by one piece of code. Rules that compute values from
   hardware (buffer pool, redo size) stay as code.

## What stays code

- **Method:** measuring, profiling and classifying windows; the evidence gate and Amdahl check; verdicts against
  noise; confirmations; steady-state and environment checks.
- **Safety:** trade-off policy, memory and swap limits, rollback, restarts, apply order.
- **Computation** from hardware facts (sizes, counts).

## Rules for learned data

- **Scope:** every learned entry has conditions, sessions / hosts and a repeatability status. Agents apply only
  entries matching their conditions. Shipped data is the fallback.
- **Influence:** learned data can rank, choose values and explain. It cannot skip measurement, and it cannot override
  a guardrail.
- **Provenance:** every decision records what influenced it (shipped vs learned entry id), so a bad learning can be
  found and retired.

## Status

- **Parameter catalog (2026-10-20): done.**
  - Shipped file `agent/tuning_agent/knowledge_data/parameters.json`, built by `tools/build_parameter_catalog.py`:
    161 settings with impact, when it counts, precondition, safety, restart, mechanisms and rules.
  - Learned impact per setting from single-setting experiments (`learning.db` `param_evidence`, per session, flawed
    runs excluded, pre-fix sessions at half weight).
  - The effective impact (learned when measured, else shipped) shifts experiment ranking: high +1, low −1.
  - Tune / block policy: admin service defaults, per-user lists (web and API), enforced by the agent and the LLM
    sanitizer.
  - Parameters page for every user.
- **Next:** mechanism effects, value grids, profile classification, baseline memory (above).
