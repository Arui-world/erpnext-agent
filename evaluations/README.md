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
failed its thresholds, `2` means the scenario file itself was invalid, and `3` means an online
suite was requested but its environment prerequisites are missing.

This offline executor measures deterministic routing, code-level tool isolation, draft argument
validation, and MCP envelope fail-closed behavior. It does **not** measure live model quality,
ERPNext data accuracy, OAuth permission differences, or end-to-end latency. Those are covered by
the authenticated online suite below, which produces a separate report.

The scenario suite is also copied into the production image. A read-only Compose container can
run it and emit JSON to stdout without accessing any external service:

```bash
docker compose run --rm --no-deps agent \
  python -m erpnext_agent.evaluation.runner
```

## Authenticated online suite (`online_authenticated_v1.json`)

The online executor drives the **running** deployment with real per-user OAuth identities. It
measures live model tool selection, ERPNext data handling, OAuth permission boundaries, and
end-to-end draft HITL flows. It requires PostgreSQL, Redis, the Agent service and the ERPNext
MCP endpoint, so it runs inside the compose network (never `--no-deps`).

Prerequisites before running:

1. The stack is up (`make up`) and the Agent is healthy.
2. Both evaluation users have logged in through the browser OAuth flow, so their credentials
   exist in PostgreSQL. The suite fails closed (exit `3`) if a required identity has no active
   credential.
3. The `EVAL_ONLINE_*` variables are set in `.env` (see `.env.example`). Fixture values
   (company, customer, supplier, item, warehouse) are injected into scenario messages at runtime.

Run it with:

```bash
make eval-online
```

Approved end-to-end draft cases create a real ERPNext draft, verify the readback shows
`docstatus=0`, then delete the draft via a **test-only** REST cleanup path that uses the owning
user's own token. This cleanup path is separate from the Agent's production path and the MCP tool
allowlist. Any draft that cannot be deleted is reported as a leftover warning for manual
follow-up.

The online report carries its own disclaimer and is intentionally **not comparable** to the
offline deterministic baseline: pass thresholds reflect live model behavior and fixture data, not
code-policy correctness.
