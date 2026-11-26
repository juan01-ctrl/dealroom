"""Measure synthetic mail admission and queue claims in an isolated PostgreSQL schema."""

import argparse
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from time import perf_counter
from uuid import uuid4

from psycopg import sql
from psycopg.conninfo import make_conninfo

from app.identity import IdentityClaims, IdentityStore
from app.mail_delivery import InboundMail, MailStore
from app.storage import Store, pooled_store


def run(store, messages, workers, tenants, duplicate_every):
    mail = MailStore(store)
    routes = []
    for number in range(tenants):
        member = IdentityStore(store).provision_workspace(
            IdentityClaims("https://load.example.test", str(number), f"creator{number}@example.test")
        )
        thread = store.create_thread(member.tenant_id, "Synthetic load campaign")
        address = f"campaign-{thread}@example.test"
        mail.configure_route(
            member.tenant_id,
            thread,
            provider="load",
            inbound_address=address,
            sender="creator@example.test",
            recipient="brand@example.test",
        )
        routes.append(address)
    payloads = [
        InboundMail(
            provider="load",
            event_id=f"mail-{number}",
            to=routes[number % len(routes)],
            from_address="brand@example.test",
            sender_authenticated=True,
            subject="Synthetic load test",
            body="USD 2,400 for one reel.",
        )
        for number in range(messages)
    ]
    attempts = [
        event
        for i, payload in enumerate(payloads)
        for event in ([payload, payload] if i % duplicate_every == 0 else [payload])
    ]

    def admit(payload):
        start = perf_counter()
        result = mail.ingest(payload, quota=messages)
        return result["job_id"], (perf_counter() - start) * 1000

    start = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        acknowledged = list(pool.map(admit, attempts))
    elapsed = perf_counter() - start

    def drain(number):
        claimed = []
        while job := store.claim(f"load-worker-{number}", lane="analysis", real_allowed=True):
            assert store.finish(job, immediate=True)
            claimed.append(job["id"])
        return claimed

    start = perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        claimed = [item for batch in pool.map(drain, range(workers)) for item in batch]
    drain_seconds = perf_counter() - start
    unique = {job for job, _ in acknowledged}
    assert len(acknowledged) == len(attempts) and len(unique) == messages
    assert len(claimed) == messages and set(claimed) == unique

    samples = sorted(duration for _, duration in acknowledged)
    return {
        "tenants": tenants,
        "concurrency": workers,
        "acknowledged": len(acknowledged),
        "unique_jobs": len(unique),
        "replays": len(attempts) - len(unique),
        "claimed_jobs": len(claimed),
        "db_admission_p95_ms": round(samples[math.ceil(0.95 * len(samples)) - 1], 1),
        "db_admission_requests_per_second": round(len(attempts) / elapsed, 1),
        "claim_and_ack_seconds": round(drain_seconds, 2),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--messages", type=int, default=600)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--tenants", type=int, default=12)
    parser.add_argument("--duplicate-every", type=int, default=5)
    args = parser.parse_args()
    database = os.environ.get("LOAD_TEST_DATABASE_URL")
    if not database or not 1 <= args.tenants <= args.messages <= 10_000 or not 1 <= args.workers <= 32:
        parser.error("Use a dedicated LOAD_TEST_DATABASE_URL and valid workload size.")
    if args.duplicate_every < 1:
        parser.error("--duplicate-every must be positive.")

    admin = Store(database)
    schema = f"dealroom_load_{uuid4().hex}"
    name = sql.Identifier(schema)
    with admin.connect() as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(name))
    try:
        with pooled_store(
            make_conninfo(database, options=f"-c search_path={schema}"), max_size=args.workers
        ) as store:
            store.migrate()
            result = run(store, args.messages, args.workers, args.tenants, args.duplicate_every)
            stats = store.pool.get_stats()
            result["pool_connections_opened"] = stats.get("connections_num", 0)
            result["pool_request_errors"] = stats.get("requests_errors", 0)
            print(json.dumps(result, indent=2))
    finally:
        with admin.connect() as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(name))


if __name__ == "__main__":
    main()
