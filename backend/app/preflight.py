import argparse
import os

from .configuration import ConfigurationError, validate_configuration
from .models import configured_model
from .observability import langfuse_config
from .storage import Store


def main():
    parser = argparse.ArgumentParser(description="Verify live configuration and database readiness.")
    parser.add_argument(
        "--check-model",
        action="store_true",
        help="Make one billable provider call with a synthetic offer.",
    )
    args = parser.parse_args()
    try:
        mode = validate_configuration("api")
        validate_configuration("worker")
        langfuse_config(os.environ)
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    try:
        Store(os.environ["DATABASE_URL"]).check_schema()
    except Exception as exc:  # noqa: BLE001 - avoid echoing database connection details
        raise SystemExit(
            f"Database check failed ({type(exc).__name__}). Check connectivity and run migrations."
        ) from None
    print("Configuration and PostgreSQL migrations verified.")
    if not args.check_model:
        return
    if mode != "live":
        raise SystemExit("--check-model requires MODEL_MODE=live.")
    try:
        model = configured_model(mode)
        offer, _ = model.extract("Northstar offers USD 1200 for one reel.", {})
        if offer.amount != 120000 or offer.currency != "USD" or offer.deliverables != {"reel": 1}:
            raise ValueError("Unexpected synthetic terms.")
    except Exception as exc:  # noqa: BLE001 - provider exceptions can include request data
        raise SystemExit(
            f"Live model check failed ({type(exc).__name__}). Check the model ID and project access."
        ) from None
    print("Live model extraction verified with synthetic data.")


if __name__ == "__main__":
    main()
