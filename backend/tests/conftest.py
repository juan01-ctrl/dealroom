import os
from urllib.parse import parse_qsl, quote, urlencode, urlsplit
from uuid import uuid4

import pytest
from psycopg import sql


@pytest.fixture(scope="session")
def store():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    from app.storage import Store

    parts = urlsplit(url)
    if parts.scheme not in {"postgres", "postgresql"}:
        pytest.fail("TEST_DATABASE_URL must be a PostgreSQL URI for an isolated test schema.")
    schema = "dealroom_test_" + uuid4().hex
    identifier = sql.Identifier(schema)
    original = Store(url)
    args = dict(parse_qsl(parts.query, keep_blank_values=True))
    args["options"] = f"{args.get('options', '')} -c search_path={schema}".strip()
    query = urlencode(args, quote_via=quote)
    isolated_url = f"{parts.scheme}://{parts.netloc}{parts.path}?{query}"
    with original.connect() as db:
        db.execute(sql.SQL("CREATE SCHEMA {}").format(identifier))
    try:
        isolated = Store(isolated_url)
        isolated.migrate()
        yield isolated
    finally:
        with original.connect() as db:
            db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(identifier))
