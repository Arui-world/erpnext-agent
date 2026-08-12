# ERPNext Agent evaluations

Scenario suites under `scenarios/` are versioned reviewable inputs. Generated JSON and
Markdown reports belong in `reports/` and are intentionally ignored because timestamps and
local run evidence should not create repository noise.

Run the deterministic baseline without model, ERPNext, Redis, or PostgreSQL access:

```bash
python -m erpnext_agent.evaluation.runner \
  --suite evaluations/scenarios/offline_policy_v1.json \
  --json-report evaluations/reports/offline_policy_v1.json \
  --markdown-report evaluations/reports/offline_policy_v1.md
```

Exit codes are stable: `0` means suite thresholds passed, `1` means cases ran but the suite
failed its thresholds, and `2` means the scenario file itself was invalid.

This first executor measures deterministic routing, code-level tool isolation, draft argument
validation, and MCP envelope fail-closed behavior. It does **not** measure live model quality,
ERPNext data accuracy, OAuth permission differences, or end-to-end latency. Those require
future authenticated online executors and separate reports.

The scenario suite is also copied into the production image. A read-only Compose container can
run it and emit JSON to stdout without accessing any external service:

```bash
docker compose run --rm --no-deps agent \
  python -m erpnext_agent.evaluation.runner
```
