# Decision sources: built-in rules, learnings, AI

The tuner decides each next run from three sources. Every recommendation records which ones it used, so a person
auditing a run can tell a shipped rule from a learned result from a model's suggestion.

| Source | What it is | Where it lives | Changes how |
|---|---|---|---|
| **Built-in rules** | Diagnoses, candidate rules, the mechanism map, the shipped parameter catalog, safety (trade-off policy, memory budget, evidence gate) | agent code and `agent/tuning_agent/knowledge/*.json` | releases (reviewed like code) |
| **Learnings** | What earlier sessions measured: per-idea outcomes in comparable conditions, hypothesis track record, learned parameter impact, insights | observations in `tuner.db` (every account's runs), `learning.db` (learner) | continuously, from every trial run |
| **AI** | Ideas the rules lack (LLM suggestions, bottleneck analyses) and the planner that picks the next step from all of the above | OpenRouter models through the service key | per call; prompts versioned in `server/app/llm.py` |

## Defaults

- **AI enabled (default mode `planner`):** the rules and the learnings build and score the candidate list. The planner
  chooses the next step from that list (or proposes one of its own). The code checks every choice: safety policy,
  settings that exist, memory budget, already tried. A rejected choice falls back to the rules' pick, and the rejection
  is recorded.
- **Other modes:** `when-needed` (AI ideas only when the rules run out), `always` (AI ideas at every step), `off`
  (built-in rules + learnings).
- **Learnings are always on.** They change rankings with bounded weights. They never skip measuring a change on their
  own: a "known neutral here" idea ranks last, and is measured again when the bottleneck or the commit path changes.

## What learnings change today

| Learned signal | Effect on the ranking | Gate |
|---|---|---|
| Outcomes of the same idea in comparable sessions (same workload class, CPU class, concurrency; all accounts) | up to ±3 by win/loss ratio; −(mean loss / 5) for consistent losses; −4 when ≥70% of 5+ runs lost | comparable conditions only for the strong penalties |
| Known neutral here: 3+ comparable runs, no win | −3, flagged `known_neutral` | not when re-tested after a shift |
| Hypothesis track record (did this mechanism move its component before?) | `rescore` / `credibility` up to ±4 | per component |
| Learned parameter impact (`param_evidence`) | replaces the shipped impact prior (±1) | measured evidence in these conditions |
| Insights (learner, `learning.db`) | +1 helps / −2 hurts / −1 no effect, × server-series weight | status `repeated` only |

## Provenance records

- **Each candidate** (`recommendations.items[].basis`):
  - `origin`: `rule` (with the rule id), `llm` or `user`;
  - `built_in` parts: mechanism vs measured bottleneck, shipped impact;
  - `learned` parts: experience, track record, learned impact, insight ids;
  - each part carries the score it added.
- **Each decision** (`recommendations.decision`):
  - `by`: `rules` or `planner:<model>[+reviewer:<model>]`;
  - the rules' own choice when the planner chose differently;
  - a rejected planner choice and why;
  - `learning`: how much learned data was loaded (comparable runs, hypothesis outcomes, learned parameters, insights).
    All zeros means built-in rules only.
- **UI** (run → Recommendations):
  - "decided by rules / AI planner (+ learnings)", with the learned data in a tooltip;
  - per item, "Based on: idea: rule X / AI / user", with "built-in n" and "learned n" badges that list each part and
    its score on hover, "chosen by AI", and "known neutral here".

## The learner

- **When it runs:** every 6 h, or when the admin starts it.
- **What it digests:** every closed session of every account. Canaries are left out. Free text (session names,
  workload descriptions) reaches the model only for the operator's own sessions.
- **Conditions:** an insight's conditions come from the session it was learned in (workload class, CPU class,
  concurrency, NUMA, server series), never from the model's wording.
- **Counting:** an insight's support and contradiction count only runs that changed exactly its settings (companions
  allowed). Reverts don't count.
- **Status:** `observed` (one session), `repeated` (several agree), `contested` or `contradicted`.
- **Errors:** per-session errors are recorded in the pass (`learner_runs.error`).
