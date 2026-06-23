"""Shared compile-time constants for cozempic components.

Extracted to break circular-import cycles: guard.py imports team.py at module
level, so team.py cannot import guard.py in return.  Any constant that BOTH
modules must agree on lives here.
"""

# Maximum characters of text fed into the DOTALL lazy-star block-regex scanners
# in detect_in_flight (guard.py) and extract_team_state (team.py).  Both scan
# sites MUST use the same value so the two parsers agree on the same slice of
# each message (team.py contract: "agree on the same bytes").
# 64K characters ≈ 64× the size of a real task-notification.  A notification
# beyond this cap is MISSED → the launch stays "in-flight" → the gate
# OVER-DEFERS the reload (recoverable, not UNDER-BLOCKS / SIGKILL).
# Mirrors recap.py's own DoS guard (text[:32768] / text[:8000]).
_RELOAD_GATE_SCAN_CAP: int = 65536

# Upper bound on any single-receipt numeric field (token count, byte count).
# No real Claude session can produce token or byte counts near 1 quadrillion:
# the largest context windows are ~4 M tokens; the heaviest sessions produce
# at most ~10 GB of transcript bytes (≈10^10).  10**15 is 5–12 orders of
# magnitude above any legitimate value, yet stays far below the float-overflow
# threshold (~1.8e308), so float(10**15) is exact and keeps
# "huge / small * 100" finite.  Values above this cap indicate corruption or
# tampering in a receipt file; all dashboard coercion helpers clamp to 0 or
# this value to prevent OverflowError from bubbling up to the user.
_MAX_RECEIPT_INT: int = 10 ** 15
