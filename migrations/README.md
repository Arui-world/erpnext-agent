# Database migrations

Run migrations through the application wrapper so the database URL comes from validated
environment settings and never appears in `alembic.ini`:

```bash
python -m erpnext_agent.migrate upgrade
python -m erpnext_agent.migrate check
python -m erpnext_agent.migrate current
```

The initial revision can adopt a complete schema previously created by SQLAlchemy
`create_all()`. It validates all managed tables before stamping the revision. A partial or
incompatible schema fails closed. The legacy `oauth_credentials` table is deliberately not
managed or deleted because current device-bound credentials use `oauth_device_credentials`.

Revision `20260812_0002` adds persistent conversation titles and a soft-deletion timestamp.
The application hides deleted conversations immediately; the retention worker later
physically removes expired conversation rows and their cascade-owned chat messages. Action
records are independent audit data and are never removed by chat retention.

Database migrations are forward-only. Restore a database backup for schema rollback.
