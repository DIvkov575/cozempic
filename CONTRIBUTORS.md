# Contributors

Cozempic is built and maintained by [Ruya AI](https://github.com/Ruya-AI), with
deep thanks to the community members whose code, reports, and ideas have shaped
the tool.

## Code

- **[@ynaamane](https://github.com/ynaamane)** — the most prolific contributor to
  cozempic (20+ merged PRs) and the backbone of its reliability work. Among many
  others: the guard crash-prevention pack (HARD-loop bound, cross-process daemon
  lock, idempotent hooks — #92/#93), the transient-daemon race + reload-chain
  hardening (#94), PID-identity / recycled-live-PID resurrection fixes (#86/#98),
  the 3GB/22h memory-leak fix via incremental transcript ingest (#89), prune-safety
  defense-in-depth (#114), watcher/recap/digest audits (#84/#104/#107), 1M-context
  scaling (#20/#28), and the numeric input-validation hardening across CLI flags +
  env vars (#83) — continued in #116. cozempic's guard is as robust as it is largely
  because of this work.

- One-off code contributions from **[@GravyaDev](https://github.com/GravyaDev)**,
  **[@schuay](https://github.com/schuay)**, **[@nullbio](https://github.com/nullbio)**,
  **[@iBoostAI](https://github.com/iBoostAI)**, and **[@carlkibler](https://github.com/carlkibler)**,
  among others — thank you.

## Design & reports

- **[@AndrewChemis](https://github.com/AndrewChemis)** — designed the interactive
  "prune now?" nudge (tiered context heads-up + reload-at-a-breakpoint) and the
  idle poll back-off / no-op accounting that make the guard genuinely useful for
  interactive sessions, including a dependency-free reference implementation and
  measured data motivating the change (#115). Also surfaced earlier guard-reliability
  issues (#106).

If you've contributed and aren't listed (or are mis-listed), please open a PR or an
issue — omissions are accidental, not intentional.
