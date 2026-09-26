# Experiment integrity policy

- Lock hypothesis, Claim revision, data/splits, baselines, metrics, seeds, ablations, statistics, exclusions, and outcome rules before results are visible.
- Before claiming preregistration or outcome blindness, inspect the pinned ContextBundle itself for prior runs, metrics, replay results, or outcome-bearing Reviews. If any are present, call the successor post-hoc/retrospective, enumerate the visible evidence, and forbid confirmatory inference; freezing later execution rules does not erase prior visibility.
- Compute `protocol_lock.sha256` as SHA-256 of canonical sorted-key compact JSON containing `hypothesis`, `method`, `baselines`, `datasets`, `metrics`, `configuration`, `interpretation_plan`, and `code_location`.
- Create a new Experiment for scientific protocol changes after READY. Record engineering fixes as run deviations.
- Pin every run to protocol revision/hash, code commit, data/config/environment hashes, and a stable run ID.
- Preserve failed, timed-out, negative, and contradictory runs and raw outputs.
- Keep execution descriptive. Only an independent verification session interprets outcomes.
- Use preregistered power, seed, and threshold choices; never import generic fixed defaults.
