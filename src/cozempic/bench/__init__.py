"""Offline benchmarking for cozempic (no live session, no LLM calls).

Two tiers:
- compression (this package's ``compression`` module): dry-run prunes over a
  corpus of saved session JSONLs; measures token/byte reclaim, per-prescription,
  plus an A/B of the fixed early-checkpoint tier and a tier-firing replay.
- swebench (see ``bench/swebench*``): task-outcome A/B (cozempic on/off).
"""
