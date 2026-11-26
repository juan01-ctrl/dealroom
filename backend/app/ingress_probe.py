"""Run a paced signed-ingress drill against authorized synthetic mail routes."""

import argparse
import hashlib
import hmac
import json
import math
import os
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import local
from time import monotonic, sleep, time
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID, uuid4

from .mail_delivery import InboundMail

_threads = local()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def ingress_url(base_url: str) -> str:
    url = urlsplit(base_url)
    local = url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        (url.scheme != "https" and not local)
        or not url.hostname
        or url.username is not None
        or url.query
        or url.fragment
        or url.path not in {"", "/"}
    ):
        raise ValueError("Use the staging API HTTPS origin or a loopback HTTP origin.")
    return base_url.rstrip("/") + "/integrations/mail/inbound"


def signed_request(url: str, secret: bytes, event: InboundMail, timestamp: int) -> Request:
    raw = event.model_dump_json().encode("utf-8")
    stamp = str(timestamp)
    signature = hmac.new(secret, b"dealroom-inbound-v1." + stamp.encode() + b"." + raw, hashlib.sha256)
    return Request(
        url,
        data=raw,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Mail-Timestamp": stamp,
            "X-Mail-Signature": signature.hexdigest(),
        },
    )


def submit(url, secret, event):
    if not hasattr(_threads, "opener"):
        _threads.opener = build_opener(ProxyHandler({}), NoRedirect())
    started = monotonic()
    job_id = None
    try:
        request = signed_request(url, secret, event, int(time()))
        with _threads.opener.open(request, timeout=10) as response:
            status = str(response.status)
            receipt = json.loads(response.read(2048))
            if status == "202":
                job_id = UUID(receipt["job_id"])
    except HTTPError as error:
        status = str(error.code)
        error.close()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        status = "unconfirmed"
    return event.event_id, status, job_id, (monotonic() - started) * 1000


def run_probe(url, secret, events, *, rate, workers):
    start = monotonic()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = []
        for index, event in enumerate(events):
            sleep(max(0, start + index / rate - monotonic()))
            futures.append(pool.submit(submit, url, secret, event))
        outcomes = [future.result() for future in futures]
    statuses = Counter()
    receipts = defaultdict(set)
    latency = []
    for event_id, status, job_id, elapsed in outcomes:
        statuses[status] += 1
        if job_id is not None:
            receipts[event_id].add(job_id)
            latency.append(elapsed)
    unique = len({event.event_id for event in events})
    unique_jobs = len(set().union(*receipts.values()))
    latency.sort()
    return {
        "requests": len(events),
        "unique_events": unique,
        "statuses": dict(statuses),
        "unique_jobs": unique_jobs,
        "events_without_receipt": unique - len(receipts),
        "events_with_multiple_receipts": sum(len(jobs) > 1 for jobs in receipts.values()),
        "accepted_p95_ms": round(latency[math.ceil(len(latency) * 0.95) - 1], 1) if latency else None,
        "passed": statuses["202"] == len(events)
        and len(receipts) == unique_jobs == unique
        and all(len(jobs) == 1 for jobs in receipts.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--routes", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=5000)
    parser.add_argument("--rate", type=int, default=20)
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--duplicate-every", type=int, default=5)
    args = parser.parse_args()
    secret = os.environ.get("MAIL_INBOUND_SECRET", "")
    provider = os.environ.get("MAIL_PROVIDER", "")
    if len(secret) < 32 or not secret.isascii():
        parser.error("Load the authorized staging MAIL_INBOUND_SECRET from the secret store.")
    if not (
        1 <= args.messages <= 10_000
        and 1 <= args.rate <= 200
        and 1 <= args.workers <= 64
        and args.duplicate_every > 0
    ):
        parser.error("Provide valid test dimensions.")
    try:
        url = ingress_url(args.origin)
        routes = json.loads(args.routes.read_text())
        if not isinstance(routes, list) or not routes:
            raise ValueError
        run_id = uuid4()
        events = []
        for index in range(args.messages):
            route = routes[index % len(routes)]
            event = InboundMail(
                provider=provider,
                event_id=f"staging-drill-{run_id}-{index}",
                to=route["to"],
                from_address=route["from_address"],
                sender_authenticated=True,
                subject="Synthetic Dealroom ingress drill",
                body="We offer USD 2,400 for one reel.",
            )
            events.append(event)
            if index % args.duplicate_every == 0:
                events.append(event)
    except (OSError, TypeError, ValueError, KeyError):
        parser.error("Provide an allowed origin and a JSON array of enrolled synthetic routes.")
    report = run_probe(url, secret.encode("ascii"), events, rate=args.rate, workers=args.workers)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["passed"] else 1)


if __name__ == "__main__":
    main()
