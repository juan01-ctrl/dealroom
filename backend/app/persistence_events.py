"""Write an audit event in the command transaction."""

from psycopg.types.json import Jsonb


def event(db, tenant, thread, revision, kind, **details):
    db.execute(
        "INSERT INTO timeline(tenant_id,thread_id,revision,kind,details) VALUES (%s,%s,%s,%s,%s)",
        (tenant, thread, revision, kind, Jsonb(details)),
    )
