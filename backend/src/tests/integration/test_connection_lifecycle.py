#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Every database session is the caller's own, and it dies with the invocation.

Drives the real provider-Lambda handler. The connection rows are in DynamoDB
Local, the credentials in a Secrets Manager emulator, and the databases are real
Oracle and Postgres, so what is asserted is the identity the DATABASE reports --
not that a function was called with an argument.
"""

import time

import pytest

from adam.data_source_provider.provider_handler import handler
from tests.integration import seed
from tests.integration.conftest import admin_password

pytestmark = pytest.mark.integration

ALICE, BOB = "user-alice", "user-bob"


@pytest.fixture(params=["oracle", "postgres"])
def target(request, registry, oracle, postgres):
    """(engine, add_connection(user, db_user), whoami_sql, sessions_of(db_user))"""
    if request.param == "oracle":
        import oracledb

        def add(user, db_user):
            password = oracle.owner_password if db_user == seed.OWNER else oracle.analyst_password
            return registry.add(user, "oracle", db_user, password, dsn=oracle.dsn, database="FREEPDB1")

        def sessions(db_user):
            with oracledb.connect(user="system", password=admin_password("oracle"), dsn=oracle.dsn) as c:
                with c.cursor() as cur:
                    cur.execute("SELECT COUNT(*) FROM v$session WHERE username = :u", u=db_user.upper())
                    return cur.fetchone()[0]

        return "oracle", add, "SELECT USER AS who FROM dual", sessions

    import pg8000.native

    def add(user, db_user):
        password = postgres.owner_password if db_user == seed.OWNER else postgres.analyst_password
        return registry.add(user, "postgres", db_user, password, host=postgres.host,
                            port=postgres.port, database=postgres.database)

    def sessions(db_user):
        c = pg8000.native.Connection(user="postgres", password=admin_password("postgres"),
                                     host=postgres.host, port=postgres.port, database=postgres.database)
        try:
            return c.run("SELECT COUNT(*) FROM pg_stat_activity WHERE usename = :u", u=db_user)[0][0]
        finally:
            c.close()

    return "postgres", add, "SELECT current_user AS who", sessions


def _call(user_id, connection_id, statement):
    # The dispatcher sends the database's limits and TLS setting with every statement;
    # the test containers have no TLS listener.
    return handler({"operation": "execute_statement", "args": {
        "user_id": user_id, "connection_id": connection_id, "statement": statement,
        "timeout_seconds": 30, "row_cap": 1000, "tls": False}}, None)


def _who(response) -> str:
    return next(iter(response["response"]["rows"][0].values())).lower()


def _wait_for_sessions(sessions, db_user, expected, seconds=10):
    """Engines report a closed session a moment after the client hangs up."""
    deadline = time.monotonic() + seconds
    while sessions(db_user) != expected and time.monotonic() < deadline:
        time.sleep(0.25)
    return sessions(db_user)


class TestEachCallerIsTheirOwnDatabaseUser:

    def test_two_callers_get_two_database_identities(self, target):
        _, add, whoami, _ = target
        alice = add(ALICE, seed.ANALYST)
        bob = add(BOB, seed.OWNER)
        assert _who(_call(ALICE, alice, whoami)) == seed.ANALYST
        assert _who(_call(BOB, bob, whoami)) == seed.OWNER

    def test_a_caller_cannot_use_another_callers_connection(self, target):
        _, add, whoami, sessions = target
        alices = add(ALICE, seed.ANALYST)
        response = _call(BOB, alices, whoami)
        assert "error" in response
        assert "response" not in response
        # Nothing was opened on Alice's behalf, or Bob's.
        assert sessions(seed.ANALYST) == 0

class TestCredentialsAreReadOnEveryCall:

    def test_a_rotated_secret_takes_effect_on_the_next_call(self, target, registry):
        """The regression: a cached session kept answering as the old database
        user for as long as the container lived, so rotating or revoking a
        credential did nothing."""
        engine, add, whoami, _ = target
        connection = add(ALICE, seed.ANALYST)
        assert _who(_call(ALICE, connection, whoami)) == seed.ANALYST

        owner_password = (seed.ORACLE_PASSWORDS if engine == "oracle" else seed.POSTGRES_PASSWORDS)[seed.OWNER]
        registry.rotate(ALICE, connection, seed.OWNER, owner_password)
        assert _who(_call(ALICE, connection, whoami)) == seed.OWNER

    def test_a_wrong_password_is_an_error_not_a_stale_session(self, target, registry):
        _, add, whoami, _ = target
        connection = add(ALICE, seed.ANALYST)
        assert _who(_call(ALICE, connection, whoami)) == seed.ANALYST
        registry.rotate(ALICE, connection, seed.ANALYST, "not-the-password")
        # The driver's login failure comes back as the error; the old session is not reused.
        answer = _call(ALICE, connection, whoami)["response"]
        assert answer["status"]["code"] == 500
        assert "rows" not in answer


class TestTheSessionDiesWithTheInvocation:
    """Every provider the handler opens is kept referenced, so its session can end only by
    the handler's own close, not by Python collecting the dropped object."""

    @pytest.fixture(autouse=True)
    def kept(self, monkeypatch):
        from contextlib import contextmanager

        from adam.data_source_provider import connection_opening

        opened, real = [], connection_opening.connection_for

        @contextmanager
        def keeping(*args, **kwargs):
            with real(*args, **kwargs) as (provider, connection):
                opened.append(provider)
                yield provider, connection
        monkeypatch.setattr(connection_opening, "connection_for", keeping)
        return opened

    def test_no_session_is_left_after_a_successful_call(self, target):
        _, add, whoami, sessions = target
        connection = add(ALICE, seed.ANALYST)
        _call(ALICE, connection, whoami)
        assert _wait_for_sessions(sessions, seed.ANALYST, 0) == 0

    def test_no_session_is_left_after_a_failing_statement(self, target):
        _, add, _, sessions = target
        connection = add(ALICE, seed.ANALYST)
        response = _call(ALICE, connection, "SELECT * FROM table_that_does_not_exist")
        assert response["response"]["status"]["code"] == 500
        assert _wait_for_sessions(sessions, seed.ANALYST, 0) == 0
