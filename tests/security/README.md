# Security tests

Deterministic security policy cases now live in
`evaluations/scenarios/offline_policy_v1.json` and cover forbidden intents, tool isolation,
client idempotency-key injection, incomplete drafts, and MCP business failures.

This directory remains reserved for authenticated two-user and fault-injection tests that
cannot be proven offline: OAuth/MCP permission differences, cross-user runtime isolation,
refresh races, write-timeout reconciliation, and prompt injection through real ERP data.
