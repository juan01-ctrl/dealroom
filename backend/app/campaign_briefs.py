"""A creator-workspace registry for operator-reviewed campaign terms."""

import argparse
import os
import re
from contextlib import contextmanager
from functools import partial
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from psycopg import Error, errors
from psycopg.types.json import Jsonb
from pydantic import Field, ValidationError, field_validator, model_validator

from .domain import Input
from .storage import Conflict, NotFound, Store

IDENTIFIER = r"^[a-z0-9]+(?:-[a-z0-9]+)*$"
Identifier = Annotated[str, Field(max_length=80, pattern=IDENTIFIER)]
Revision = Annotated[int, Field(ge=1, le=2_147_483_647)]
Duration = Annotated[int, Field(ge=1, le=3650)]
MAX_INPUT_BYTES = 16_384
# Only an exact HTTPS document reference is stored. Credentials, queries and fragments are excluded.
HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
SOURCE_URL = re.compile(rf"https://(?:{HOST_LABEL}\.)+[a-z]{{2,63}}/[A-Za-z0-9_./~%-]*")


class BriefSection(Input):
    id: Identifier
    exclusive_days: Duration | None = None
    usage_days: Duration | None = None
    usage_scope: Literal["paid social", "paid search", "display ads"] | None = None

    @model_validator(mode="after")
    def require_terms(self):
        if all(value is None for value in (self.exclusive_days, self.usage_days, self.usage_scope)):
            raise ValueError("Each section needs at least one supported commercial term.")
        return self


class BriefRevision(Input):
    document_id: Identifier
    title: str = Field(min_length=1, max_length=160)
    revision: Revision
    sections: list[BriefSection] = Field(min_length=1, max_length=16)

    @field_validator("title")
    @classmethod
    def require_title(cls, value):
        if not value.strip() or not value.isprintable():
            raise ValueError("Enter a printable title.")
        return value

    @model_validator(mode="after")
    def unique_sections(self):
        if len({section.id for section in self.sections}) != len(self.sections):
            raise ValueError("Section IDs must be unique within the revision.")
        return self


class BriefRegistration(BriefRevision):
    source_url: str = Field(max_length=512)

    @field_validator("source_url")
    @classmethod
    def require_reference(cls, value):
        if not SOURCE_URL.fullmatch(value) or value.endswith("."):
            raise ValueError("Use an exact HTTPS document URL without credentials, query, or fragment.")
        return value


class CampaignBriefs:
    def __init__(self, store):
        self.store = store

    def register(self, tenant, brief: BriefRegistration):
        sections = [section.model_dump(exclude_none=True) for section in brief.sections]
        key = (tenant, brief.document_id)
        try:
            with self.store.connect() as db:
                if not db.execute(
                    "SELECT 1 FROM creator_workspaces WHERE tenant_id=%s FOR KEY SHARE", (tenant,)
                ).fetchone():
                    raise NotFound("Creator workspace not found.")
                db.execute(
                    """INSERT INTO campaign_briefs(tenant_id,document_id,source_url) VALUES (%s,%s,%s)
                       ON CONFLICT (tenant_id,document_id) DO NOTHING""",
                    (*key, brief.source_url),
                )
                document = db.execute(
                    """SELECT source_url,revoked_at FROM campaign_briefs
                       WHERE tenant_id=%s AND document_id=%s FOR UPDATE""",
                    key,
                ).fetchone()
                if document["revoked_at"] or document["source_url"] != brief.source_url:
                    raise Conflict("This document ID has a different reference or has been revoked.")
                revision = db.execute(
                    """SELECT title,sections FROM campaign_brief_revisions
                       WHERE tenant_id=%s AND document_id=%s AND revision=%s""",
                    (*key, brief.revision),
                ).fetchone()
                if revision:
                    if revision != {"title": brief.title, "sections": sections}:
                        raise Conflict("A registered revision cannot change.")
                    return
                latest = db.execute(
                    """SELECT coalesce(max(revision),0) AS revision FROM campaign_brief_revisions
                       WHERE tenant_id=%s AND document_id=%s""",
                    key,
                ).fetchone()["revision"]
                if brief.revision != latest + 1:
                    raise Conflict("Use the next consecutive revision number.")
                db.execute(
                    """INSERT INTO campaign_brief_revisions(tenant_id,document_id,revision,title,sections)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (*key, brief.revision, brief.title, Jsonb(sections)),
                )
        except errors.UniqueViolation:
            raise Conflict("This reference is already registered in the workspace.") from None

    def revoke(self, tenant, document_id):
        with self.store.connect() as db:
            if not db.execute(
                """UPDATE campaign_briefs SET revoked_at=coalesce(revoked_at,now())
                   WHERE tenant_id=%s AND document_id=%s RETURNING document_id""",
                (tenant, document_id),
            ).fetchone():
                raise NotFound("Campaign brief not found.")

    @contextmanager
    def reader(self, tenant, thread):
        with self.store.connect() as db:
            db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            scope = db.execute(
                """SELECT w.tenant_id IS NOT NULL AS creator FROM negotiations n
                   LEFT JOIN creator_workspaces w ON w.tenant_id=n.tenant_id
                   WHERE n.tenant_id=%s AND n.id=%s""",
                (tenant, thread),
            ).fetchone()
            if scope is None:
                raise NotFound("Negotiation not found.")
            yield partial(self._lookup, db, tenant) if scope["creator"] else None

    @staticmethod
    def _lookup(db, tenant, url):
        if len(url) > 512 or not SOURCE_URL.fullmatch(url):
            return None
        return db.execute(
            """SELECT r.document_id,r.revision,r.title,r.sections
               FROM campaign_briefs b JOIN campaign_brief_revisions r USING (tenant_id,document_id)
               WHERE b.tenant_id=%s AND b.source_url=%s AND b.revoked_at IS NULL
               ORDER BY r.revision DESC LIMIT 1""",
            (tenant, url),
        ).fetchone()

    def inspect(self, tenant, thread, document_id, revision):
        with self.store.connect() as db:
            document = db.execute(
                """SELECT r.document_id,r.revision,r.title,r.sections
                   FROM negotiations n JOIN campaign_briefs b ON b.tenant_id=n.tenant_id
                   JOIN campaign_brief_revisions r ON (r.tenant_id,r.document_id)=(b.tenant_id,b.document_id)
                   WHERE n.tenant_id=%s AND n.id=%s AND b.document_id=%s
                     AND r.revision=%s AND b.revoked_at IS NULL""",
                (tenant, thread, document_id, revision),
            ).fetchone()
        if not document:
            raise NotFound("Campaign brief not found.")
        return document


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    register = commands.add_parser("register", help="Register an immutable brief revision.")
    register.add_argument("workspace", type=UUID)
    register.add_argument("file", type=Path)
    revoke = commands.add_parser("revoke", help="Revoke a document and its revisions.")
    revoke.add_argument("workspace", type=UUID)
    revoke.add_argument("document_id")
    args = parser.parse_args()

    if not (database_url := os.getenv("DATABASE_URL")):
        parser.exit(1, "Set DATABASE_URL for the target environment.\n")
    briefs = CampaignBriefs(Store(database_url))
    try:
        if args.command == "register":
            with args.file.open("rb") as file:
                raw = file.read(MAX_INPUT_BYTES + 1)
            if len(raw) > MAX_INPUT_BYTES:
                raise ValueError("Brief JSON exceeds 16 KiB.")
            briefs.register(args.workspace, BriefRegistration.model_validate_json(raw))
        else:
            briefs.revoke(args.workspace, args.document_id)
    except ValidationError as exc:
        parser.exit(1, "Invalid brief JSON. " + str(exc.error_count()) + " validation error(s).\n")
    except Error as exc:
        parser.exit(1, f"Brief database command failed ({type(exc).__name__}).\n")
    except OSError:
        parser.exit(1, "Brief JSON file could not be read.\n")
    except (Conflict, NotFound, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
