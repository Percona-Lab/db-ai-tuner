
## Parameter interactions (agreed 2026-10-19; build with the Battery 1 improvements)

Problem: the tuner changes one setting at a time from the best known configuration. A change rejected in one context
(e.g. `join_buffer_size=8M` with AHI off) is never retried after the context changes (AHI kept later). Documentation lists
no interactions; research (OtterTune: Lasso with pairwise knob products, GP surrogates; fANOVA / SHAP importance studies)
learns them from many measured configurations.

1. **Context re-test**: interaction groups from the mechanism map (latching/concurrency, redo/flush, memory,
   allocator/THP, optimizer). When a change is kept, rejected experiments in the same group get one more run.
2. **Fractional factorial at the end**: top 4-7 candidates (kept + near-noise) in a 2-level fractional factorial
   (8 runs, resolution III; 16 runs resolution IV) -> main effects + strongest 2-way interactions, best combination confirmed.
3. **Interaction memory across sessions (learn from data)**: store the state of related settings with every experiment;
   accumulate per workload type the effect of X with Y on vs off; use as priors for (1). Grows with every battery.
   First look at existing data (2026-10-19): 41 experiment x context pairs seen in both states, confounded by
   workload / machine (e.g. thread pool -11..-18% with log_writer_threads=OFF vs ~-2% with ON) - suggestive only.
4. **Battery 2 targeted factorials**: join_buffer x AHI (TPC-H), log_writer_threads x flush_log_at_trx_commit (OLTP write),
   allocator x THP (1024 connections) to measure interaction sizes on clean data.
