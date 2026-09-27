"""Proposal <-> opportunity matching (docs/10 US-401..403; docs/21 §3.11 `match`).

`rules.py` loads the versioned rule set (`data/match_rules.yaml`), `engine.py` scores one pair as
pure functions, `run.py` recomputes the `match` table and writes the `match_added` /
`match_removed` events, `eval.py` measures the rule set against `data/eval/match_labels.csv`.
"""
