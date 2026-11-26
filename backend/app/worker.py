import argparse
import logging
import os
import signal
import time
from threading import Event
from uuid import UUID, uuid4

from langgraph.checkpoint.postgres import PostgresSaver

from .configuration import ConfigurationError, daily_model_budget, validate_configuration
from .identity import is_creator_workspace
from .mail_delivery import MailStore, configured_mail_provider
from .maintenance import expire_sessions
from .models import configured_model
from .observability import configured_langfuse, langfuse_config, observe
from .operations import LANES, execution_identity, maintain_heartbeat, maintain_lease
from .storage import ModelBudgetExhausted, pooled_store
from .workflow import PredecessorRecoveryRequired, SimulatedFailure, run_analysis

logger = logging.getLogger(__name__)


class Worker:
    def __init__(
        self, store, model=None, *, immediate=False, lane="all", mail_provider=None, real_analysis=False
    ):
        self.store = store
        self.model = model if lane == "delivery" else model or configured_model()
        self.worker_id = str(uuid4())
        self.immediate = immediate
        self.lane = lane
        self.identity = execution_identity(self.model.mode if self.model else None)
        self.mail_provider = mail_provider
        self.real_allowed = mail_provider is not None or (lane == "analysis" and real_analysis)

    def step(self, identifier=None):
        job = self.store.claim(
            self.worker_id,
            identifier=identifier,
            lease=90,
            lane=self.lane,
            identity=self.identity,
            real_allowed=self.real_allowed,
        )
        if not job:
            return False
        with self.store.connect(autocommit=True) as lock:
            locked = lock.execute(
                "SELECT pg_try_advisory_lock(hashtextextended(%s,0)) AS acquired", (str(job["id"]),)
            ).fetchone()["acquired"]
            if not locked:
                self.store.finish(job, busy=True)
                return True
            trace_context = {
                "job_id": str(job["id"]),
                "attempt": job["attempts"],
                "lane": self.lane,
                **self.identity,
            }
            if thread_id := job["thread_id"]:
                trace_context.update(thread_id=str(thread_id), revision=job["revision"])
            with observe(f"job.{job['kind']}", as_type="chain", metadata=trace_context) as span:
                try:
                    with maintain_lease(self.store, job):
                        self.process(job, trace_id=span.trace_id)
                except ModelBudgetExhausted:
                    span.update(level="WARNING", status_message="ModelBudgetExhausted")
                    self.store.outbox.defer_model_budget(job)
                except PredecessorRecoveryRequired:
                    span.update(level="WARNING", status_message="PredecessorRecoveryRequired")
                    self.store.finish(job, retry=True, terminal=True, immediate=self.immediate)
                except SimulatedFailure:
                    span.update(level="WARNING", status_message="SimulatedFailure")
                    self.store.finish(job, retry=True, simulated=True, immediate=self.immediate)
                except Exception as exc:  # noqa: BLE001 - process boundary
                    span.update(level="ERROR", status_message=type(exc).__name__)
                    logger.warning(
                        "Job %s failed (%s); Langfuse trace %s",
                        job["id"],
                        type(exc).__name__,
                        span.trace_id,
                    )
                    self.store.finish(job, retry=True, immediate=self.immediate)
                else:
                    self.store.finish(job, immediate=self.immediate)
        return True

    def process(self, job, *, trace_id=None):
        if job["kind"] == "analyze":
            with self.store.connect(autocommit=True) as db:
                run_analysis(self.store, PostgresSaver(db), job, self.model, trace_id=trace_id)
        elif job["kind"] == "deliver":
            tenant, thread = job["tenant_id"], job["thread_id"]
            approval = UUID(job["payload"]["approval_id"])
            with self.store.connect() as db:
                real = is_creator_workspace(db, tenant)
            if real:
                if self.mail_provider is None:
                    raise ConfigurationError("No mail provider is configured for this workspace.")
                MailStore(self.store).deliver(tenant, thread, approval, self.mail_provider)
            else:
                _, fault = self.store.deliver(tenant, thread, approval)
                if fault:
                    raise SimulatedFailure("delivery")
        else:
            from .evaluations import execute_run

            execute_run(self.store, job["tenant_id"], UUID(job["payload"]["run_id"]), self.model)


def main():
    parser = argparse.ArgumentParser(description="Process the negotiation outbox.")
    parser.add_argument("--once", type=UUID, help="Process one specific durable job and exit.")
    parser.add_argument("--lane", choices=LANES, default="all", help="Jobs this worker can claim.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    try:
        mode = validate_configuration("delivery" if args.lane == "delivery" else "worker")
        langfuse_config(os.environ)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    with pooled_store(os.environ["DATABASE_URL"], max_size=4) as store:
        run_worker(store, args, mode)


def run_worker(store, args, mode):
    try:
        store.check_schema()
    except Exception as exc:  # noqa: BLE001 - avoid echoing database connection details
        raise SystemExit(
            f"Worker database check failed ({type(exc).__name__}). Run migrations and verify connectivity."
        ) from None
    cap = daily_model_budget(os.environ) if mode == "live" and args.lane != "delivery" else None
    reserve_call = None
    if cap is not None:

        def reserve_call():
            store.reserve_model_call(cap)

    worker = Worker(
        store,
        model=configured_model(mode, reserve_call=reserve_call) if args.lane != "delivery" else None,
        immediate=bool(args.once),
        lane=args.lane,
        mail_provider=configured_mail_provider()
        if args.lane in {"negotiation", "delivery", "all"}
        else None,
        real_analysis=os.getenv("REAL_MAIL_ENABLED") == "1",
    )
    with configured_langfuse():
        if args.once:
            raise SystemExit(0 if worker.step(args.once) else 1)
        stop = Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        with maintain_heartbeat(store, worker.worker_id, worker.lane, worker.identity):
            poll(worker, stop)


def poll(worker, stop):
    next_cleanup = time.monotonic()
    while not stop.is_set():
        try:
            if worker.lane in {"all", "negotiation", "analysis"} and time.monotonic() >= next_cleanup:
                next_cleanup = time.monotonic() + 3600
                expire_sessions(worker.store)
            worked = worker.step()
        except Exception as exc:  # noqa: BLE001 - process boundary
            logger.warning("Worker polling failed (%s)", type(exc).__name__)
            worked = False
        if not worked:
            stop.wait(0.35)


if __name__ == "__main__":
    main()
