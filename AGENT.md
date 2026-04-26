# AGENT.md

## Repository workflow expectations

- Maintain `FORK_CHANGELOG.md` as the canonical record of fork-only changes.
- On every PR that changes fork-owned behavior, update `FORK_CHANGELOG.md` in the same PR.
- Keep entries concise and factual: what changed, why it exists in the fork, and any merge risks when syncing upstream.
- When resolving upstream sync conflicts, preserve both upstream improvements and documented fork-only features unless a PR explicitly removes or replaces them.
