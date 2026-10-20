# AI DB Tuner as an agentic SaaS service

## Roles

- **Admin account:** created when the service is set up (`server/deploy/create_admin.py`), never by self-registration.
  It holds the service's OpenRouter key and model / usage settings, sees the Learning page, and runs or reviews the
  learner.
- **Users:** register freely (for now). They don't need any AI key: every AI call (suggestions, bottleneck analyses,
  learning) goes through the service key. Usage is still logged per requesting user and session.

## Code and data are separate

| Part | Where | What |
|---|---|---|
| Tuner code (OSS) | the git repository: agent, rules, mechanisms, evaluation, server | how to measure, diagnose, decide; improved by people and reviewed releases |
| Observations | `tuner.db` (service database) | environments, sessions, runs with their metrics, profiles, verdicts, hypotheses: what happened |
| Learnings | `learning.db` (separate database, never in the repository) | what the observations teach, beyond the code: insights with conditions, evidence and confidence; proposals for code changes |

The code works without `learning.db`. With it, the tuner is better informed: insights rank candidates and appear as
leads in the "why". Insights never bypass the evidence gate: measurements decide. `learning.db` can be exported and
imported (snapshot), so a fresh service can start with what an earlier one learned.

## The learner (background process)

The learner runs inside the service, every 6 hours by default, or when the admin starts it from the Learning page.

1. **Aggregate:** per-idea outcomes by conditions (workload, CPU class, concurrency class) and hypothesis outcomes
   per targeted component, recomputed from the observations.
2. **Digest:** each session closed since the last pass, summarised as the measured bottlenecks, experiments, outcomes,
   hypothesis results and environment problems.
3. **Ask the AI (service key)** for insights from that digest, plus what is already known. Each insight is a testable
   statement: a setting or mechanism, the conditions (workload, hardware, measured bottleneck components), the
   expected direction, and the sessions behind it. Where the code itself should change, the AI writes a proposal for
   a rule change, a missing measurement, or a bug.
4. **Validate** every insight against the observation data, not the AI's word: it counts the runs that support and
   contradict it under its conditions. Confidence comes from those counts. An insight with no data behind it stays a
   "candidate" (shown to agents as an unverified lead). An insight contradicted more often than supported is retired.
5. **Merge** duplicates, and keep a log of every learner pass: sessions read, calls, cost, errors.

Agents receive the insights that match their conditions through `/experience`. The engine attaches them to matching
candidates as leads (validated or not), with a small ranking prior for validated ones.

## Improving the code

Proposals collected by the learner, together with the periodic reviews (`docs/review/`), feed the OSS code updates.
A person, or Claude Code with review, changes the rules / measurements, tests them with the canary and the test
suite (`docs/battery/suite-v1.md`), and releases. The scorecard shows whether a release made the tuner better.
Insights that became code are marked "implemented" in `learning.db`.

## Guardrails

- **Data stays as measured:** the learner never edits observations, only `learning.db`.
- **Cost cap:** a per-pass and per-day limit on AI calls, logged in the learner log.
- **Evidence first:** insights shape ranking and explanations only. A change still needs measured evidence to run as
  an experiment, and measured results to be kept.
