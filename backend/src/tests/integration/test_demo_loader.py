#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The demo dataset loader, against real Oracle and Postgres.

Each run gets its own Oracle schema owner and its own Postgres database, so the
seeded schemas the other tests use are never touched.
"""

import uuid

import oracledb
import pg8000.native
import pytest

from demo_dataset import build
from tests.integration import seed
from tests.integration.conftest import admin_password

pytestmark = pytest.mark.integration

OWNER_PASSWORD = seed.ORACLE_PASSWORDS[seed.OWNER]  # generated for this run
ROLES = ("RELATIONSHIP_MANAGER", "SERVICING_ANALYST")


@pytest.fixture
def loader_oracle(oracle):
    """A fresh schema owner, plus the two database users the schema grants to."""
    owner = f"LOADER_{uuid.uuid4().hex[:6]}".upper()
    admin = oracledb.connect(user="system", password=admin_password("oracle"), dsn=oracle.dsn)
    cur = admin.cursor()
    made = []
    for role in ROLES:
        try:
            cur.execute(f'CREATE USER {role} IDENTIFIED BY "{seed.ORACLE_PASSWORDS[seed.ANALYST]}"')
            made.append(role)
        except oracledb.DatabaseError:
            pass  # already there
    cur.execute(f'CREATE USER {owner} IDENTIFIED BY "{OWNER_PASSWORD}" QUOTA UNLIMITED ON USERS')
    cur.execute(f"GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW TO {owner}")
    yield {"owner": owner, "dsn": oracle.dsn,
           "owner_credentials": {"user": owner, "password": OWNER_PASSWORD, "dsn": oracle.dsn},
           "admin_credentials": {"user": "system", "password": admin_password("oracle"), "dsn": oracle.dsn},
           "admin": admin}
    cur.execute(f"DROP USER {owner} CASCADE")
    for role in made:
        cur.execute(f"DROP USER {role}")
    admin.close()


@pytest.fixture
def loader_postgres(postgres):
    """A fresh database and owner role, plus the two roles the schema grants to."""
    name = f"loader_{uuid.uuid4().hex[:6]}"
    admin = pg8000.native.Connection(user="postgres", password=admin_password("postgres"),
                                     host=postgres.host, port=postgres.port, database=postgres.database)
    for role in ("relationship_manager", "servicing_analyst"):
        if not admin.run("SELECT 1 FROM pg_roles WHERE rolname = :r", r=role):
            admin.run(f"CREATE ROLE {role}")
    admin.run(f"CREATE ROLE {name} LOGIN PASSWORD '{OWNER_PASSWORD}'")
    admin.run(f"CREATE DATABASE {name} OWNER {name}")
    yield {"user": name, "password": OWNER_PASSWORD, "host": postgres.host, "port": postgres.port,
           "database": name, "ssl_context": None}
    admin.run(f"DROP DATABASE {name} WITH (FORCE)")
    admin.run(f"DROP ROLE {name}")
    admin.close()


def _oracle_count(creds, table) -> int:
    with oracledb.connect(**creds) as c:
        return c.cursor().execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _owner_is_read_only(loader_oracle) -> str:
    return loader_oracle["admin"].cursor().execute(
        "SELECT read_only FROM dba_users WHERE username = :u", u=loader_oracle["owner"]).fetchone()[0]


def _run(loader_oracle, loader_postgres, *, admin=True, apply=True):
    return build.run(oracle=loader_oracle["owner_credentials"],
                     oracle_admin=loader_oracle["admin_credentials"] if admin else None,
                     aurora=loader_postgres, apply=apply)


class TestLoadingFromNothing:

    def test_a_dry_run_writes_nothing(self, loader_oracle, loader_postgres):
        assert _run(loader_oracle, loader_postgres, apply=False) == 0
        with pytest.raises(oracledb.DatabaseError, match="ORA-00942"):
            _oracle_count(loader_oracle["owner_credentials"], "clients")
        pg = pg8000.native.Connection(user=loader_postgres["user"], password=loader_postgres["password"],
                                      host=loader_postgres["host"], port=loader_postgres["port"],
                                      database=loader_postgres["database"])
        try:
            assert pg.run("SELECT to_regclass('wealth.securities')") == [[None]]
        finally:
            pg.close()

    def test_it_creates_the_schema_and_loads_both_databases(self, loader_oracle, loader_postgres):
        assert _run(loader_oracle, loader_postgres) == 0
        creds = loader_oracle["owner_credentials"]
        assert _oracle_count(creds, "clients") == 500
        assert _oracle_count(creds, "client_life_events") > 0   # the table older installs lacked
        pg = pg8000.native.Connection(user=loader_postgres["user"], password=loader_postgres["password"],
                                      host=loader_postgres["host"], port=loader_postgres["port"],
                                      database=loader_postgres["database"])
        assert pg.run("SELECT COUNT(*) FROM wealth.securities")[0][0] == 40
        pg.close()

    def test_running_it_again_gives_the_same_result(self, loader_oracle, loader_postgres):
        assert _run(loader_oracle, loader_postgres) == 0
        assert _run(loader_oracle, loader_postgres) == 0
        assert _oracle_count(loader_oracle["owner_credentials"], "clients") == 500


class TestAReadOnlyOwner:
    """Registered accounts must be READ ONLY, so the owner often is; the loader has to cope."""

    def test_it_loads_and_leaves_the_owner_read_only(self, loader_oracle, loader_postgres):
        assert _run(loader_oracle, loader_postgres) == 0
        loader_oracle["admin"].cursor().execute(f"ALTER USER {loader_oracle['owner']} READ ONLY")
        assert _run(loader_oracle, loader_postgres) == 0
        assert _owner_is_read_only(loader_oracle) == "YES"
        assert _oracle_count(loader_oracle["owner_credentials"], "clients") == 500

    def test_it_restores_read_only_even_when_the_load_fails(self, loader_oracle, loader_postgres, monkeypatch):
        assert _run(loader_oracle, loader_postgres) == 0
        loader_oracle["admin"].cursor().execute(f"ALTER USER {loader_oracle['owner']} READ ONLY")
        monkeypatch.setattr(build, "load_oracle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))
        with pytest.raises(RuntimeError, match="disk full"):
            _run(loader_oracle, loader_postgres)
        assert _owner_is_read_only(loader_oracle) == "YES"

    def test_without_an_admin_credential_the_oracle_refusal_is_not_swallowed(self, loader_oracle, loader_postgres):
        assert _run(loader_oracle, loader_postgres) == 0
        loader_oracle["admin"].cursor().execute(f"ALTER USER {loader_oracle['owner']} READ ONLY")
        with pytest.raises(oracledb.DatabaseError, match="ORA-28194"):
            _run(loader_oracle, loader_postgres, admin=False)
