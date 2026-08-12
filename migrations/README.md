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

Database migrations are forward-only. Restore a database backup for schema rollback.
