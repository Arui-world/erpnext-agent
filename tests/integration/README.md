# Integration tests

This directory is reserved for authenticated online evaluations and integration tests that run
against Redis, PostgreSQL and an ERPNext MCP endpoint. Unit tests and the versioned
`offline_policy_v1` suite deliberately do not use a shared ERPNext service credential.

Online cases must declare their required fixture data and identity, use per-user OAuth, avoid
Administrator fallback, and clean temporary records. Their reports must remain distinct from
the offline deterministic policy score.
