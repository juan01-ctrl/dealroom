import stat

import pytest

from app.configuration import ConfigurationError, validate_configuration
from app.configure_live import write_live_environment


def production():
    return {
        "APP_ENV": "production",
        "DATABASE_URL": "postgresql://sandbox:fake@database.test/negotiation",
        "MODEL_MODE": "live",
        "COOKIE_SECURE": "1",
        "API_PROXY_SECRET": "x" * 32,
        "MODEL_PROVIDER": "openai",
        "MODEL_NAME": "project-model",
        "OPENAI_API_KEY": "example-only",
        "LIVE_DAILY_MODEL_CALLS": "100",
    }


def test_production_api_does_not_require_provider_credentials():
    env = production()
    del env["OPENAI_API_KEY"]
    validate_configuration("api", env)
    with pytest.raises(ConfigurationError, match="OPENAI_API_KEY"):
        validate_configuration("worker", env)


@pytest.mark.parametrize(
    "changed",
    [
        {"MODEL_MODE": "scripted"},
        {"COOKIE_SECURE": "0"},
        {"API_PROXY_SECRET": ""},
    ],
)
def test_production_api_rejects_unsafe_configuration(changed):
    with pytest.raises(ConfigurationError):
        validate_configuration("api", production() | changed)


def test_configuration_is_private_and_never_overwrites(tmp_path):
    path = tmp_path / ".env.live"
    same = {"OPENAI_API_KEY": "dummy-secret", "DATABASE_URL": "postgresql://dev@localhost/db"}
    write_live_environment(path, same)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "dummy-secret" in path.read_text()
    with pytest.raises(FileExistsError):
        write_live_environment(path, {"OPENAI_API_KEY": "replacement"})
    assert "replacement" not in path.read_text()


def test_database_budget_is_shared_between_workers(store):
    from concurrent.futures import ThreadPoolExecutor
    from uuid import uuid4

    from psycopg import sql
    from psycopg.conninfo import make_conninfo

    from app.storage import MIGRATIONS, Limit, Store

    schema = "budget_test_" + uuid4().hex
    identifier = sql.Identifier(schema)
    with store.connect() as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(identifier))
    try:
        isolated = make_conninfo(store.url, options=f"-c search_path={schema}")
        with Store(isolated).connect() as db:
            db.execute((MIGRATIONS / "005_live_model_budget.sql").read_text())

        def reserve(_):
            try:
                Store(isolated).reserve_model_call(3)
            except Limit:
                return False
            return True

        with ThreadPoolExecutor(max_workers=6) as pool:
            assert sum(pool.map(reserve, range(6))) == 3
    finally:
        with store.connect() as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))


def test_production_requires_explicit_model_budget():
    with pytest.raises(ConfigurationError, match="LIVE_DAILY_MODEL_CALLS"):
        validate_configuration("worker", production() | {"LIVE_DAILY_MODEL_CALLS": ""})
