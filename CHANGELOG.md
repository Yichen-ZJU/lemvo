# Changelog

## 2026-11-02 — Windows decision worker and SSH handoff

- Add an opt-in native Windows Codex worker and per-project SSH bridge, reusing existing Linux Lemvo controllers and ledgers.
- Add durable request claims, output acknowledgement, separate provider sessions, worker restart recovery, and safe planned Windows/server handoff.
- Add managed Windows skill installation, preserving local overrides and existing Windows GPU reservation skills.
- No active research campaigns are automatically adopted or restarted. See desktop/README.md for recovery boundaries.
