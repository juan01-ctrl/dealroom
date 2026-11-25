import os
import re
import warnings
from getpass import GetPassWarning, getpass
from pathlib import Path

from .configuration import ConfigurationError, validate_configuration


def write_live_environment(path, values):
    if any(
        not value or any(character in value for character in "'\\\r\n\0") for value in values.values()
    ):
        raise ConfigurationError(
            "Use single-line values and URL-encode special characters in DATABASE_URL."
        )
    payload = "".join(f"{key}='{value}'\n" for key, value in values.items())
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(payload)


def main():
    path = Path(__file__).resolve().parents[1] / ".env.live"
    if path.exists() or path.is_symlink():
        raise SystemExit("backend/.env.live already exists. Edit it locally to rotate credentials.")
    try:
        provider = input("Provider [openai]: ").strip().lower() or "openai"
        if provider not in {"openai", "anthropic"}:
            raise ConfigurationError("Use openai or anthropic.")
        model = input("Model ID: ").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:/-]{1,160}", model):
            raise ConfigurationError("Enter a valid provider model ID.")
        key_name = "OPENAI_API_KEY" if provider == "openai" else "ANTHROPIC_API_KEY"
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            key = getpass(f"{key_name} (hidden): ").strip()
            database_url = getpass("DATABASE_URL (hidden): ").strip()
        values = {
            "APP_ENV": "local",
            "MODEL_MODE": "live",
            "MODEL_PROVIDER": provider,
            "MODEL_NAME": model,
            key_name: key,
            "DATABASE_URL": database_url,
        }
        validate_configuration("worker", values)
        write_live_environment(path, values)
    except (GetPassWarning, KeyboardInterrupt, EOFError):
        raise SystemExit(
            "Configuration canceled. Run this command in an interactive terminal."
        ) from None
    except FileExistsError:
        raise SystemExit("backend/.env.live already exists.") from None
    except ConfigurationError as exc:
        raise SystemExit(str(exc)) from None
    print("Created backend/.env.live with owner-only access. Configuration values were not printed.")


if __name__ == "__main__":
    main()
