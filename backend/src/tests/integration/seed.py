#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Seed data for the local Oracle, Postgres, MySQL and MariaDB test containers.

Both engines get the same shape: an owner who can read and write, two tables
and a restricted view, a table the guard tests try to damage, and an analyst
whose only access is the view, granted through a role. The owner keeps full DML
rights on purpose: what stops a hostile statement in the guard tests must be the
provider, not the database privileges.
"""

import secrets

OWNER = "mcp_dev"
ANALYST = "analyst"


def _password() -> str:
    """Generated for this run: no password is written down. Upper and lower case and a
    digit, which every engine accepts, and no quote characters."""
    return "Aa1" + secrets.token_hex(12)


ORACLE_PASSWORDS = {OWNER: _password(), ANALYST: _password()}
POSTGRES_PASSWORDS = {OWNER: _password(), ANALYST: _password()}
MYSQL_PASSWORDS = {OWNER: _password(), ANALYST: _password()}

_ORACLE_DROP = """
BEGIN
  EXECUTE IMMEDIATE 'DROP {kind} {name}{cascade}';
EXCEPTION WHEN OTHERS THEN
  IF SQLCODE NOT IN (-1918, -1919) THEN RAISE; END IF;
END;"""

ORACLE_SEED = [
    _ORACLE_DROP.format(kind="USER", name="analyst", cascade=" CASCADE"),
    _ORACLE_DROP.format(kind="USER", name="mcp_dev", cascade=" CASCADE"),
    _ORACLE_DROP.format(kind="ROLE", name="analyst_role", cascade=""),
    f'CREATE USER mcp_dev IDENTIFIED BY "{ORACLE_PASSWORDS[OWNER]}" QUOTA UNLIMITED ON USERS',
    "GRANT CREATE SESSION, CREATE TABLE, CREATE VIEW TO mcp_dev",
    "CREATE TABLE mcp_dev.clients (id NUMBER PRIMARY KEY, name VARCHAR2(100) NOT NULL, email VARCHAR2(200))",
    "CREATE TABLE mcp_dev.accounts (id NUMBER PRIMARY KEY, "
    "client_id NUMBER REFERENCES mcp_dev.clients(id), balance NUMBER(12,2))",
    "CREATE TABLE mcp_dev.audit_log (id NUMBER PRIMARY KEY, note VARCHAR2(100))",
    "INSERT INTO mcp_dev.clients VALUES (1, 'Ada', 'ada@example.com')",
    "INSERT INTO mcp_dev.clients VALUES (2, 'Grace', 'grace@example.com')",
    "INSERT INTO mcp_dev.accounts VALUES (10, 1, 100.50)",
    "INSERT INTO mcp_dev.audit_log VALUES (1, 'original')",
    "CREATE VIEW mcp_dev.v_clients_restricted AS SELECT id, name FROM mcp_dev.clients",
    "COMMENT ON TABLE mcp_dev.accounts IS 'One row per client''s account'",
    "COMMENT ON COLUMN mcp_dev.accounts.balance IS 'Current balance in USD'",
    "COMMENT ON TABLE mcp_dev.v_clients_restricted IS 'Clients without contact details'",
    "CREATE ROLE analyst_role",
    "GRANT SELECT ON mcp_dev.v_clients_restricted TO analyst_role",
    f'CREATE USER analyst IDENTIFIED BY "{ORACLE_PASSWORDS[ANALYST]}"',
    "GRANT CREATE SESSION TO analyst",
    "GRANT analyst_role TO analyst",
    "COMMIT",
]

POSTGRES_SEED = [
    "DROP SCHEMA IF EXISTS wealth CASCADE",
    "DROP ROLE IF EXISTS analyst",
    "DROP ROLE IF EXISTS analyst_role",
    "DROP ROLE IF EXISTS mcp_dev",
    f"CREATE ROLE mcp_dev LOGIN PASSWORD '{POSTGRES_PASSWORDS[OWNER]}'",
    "CREATE SCHEMA wealth AUTHORIZATION mcp_dev",
    "CREATE TABLE wealth.clients (id int PRIMARY KEY, name text NOT NULL, email text)",
    "CREATE TABLE wealth.accounts (id int PRIMARY KEY, "
    "client_id int REFERENCES wealth.clients(id), balance numeric(12,2))",
    "CREATE TABLE wealth.audit_log (id int PRIMARY KEY, note text)",
    "INSERT INTO wealth.clients VALUES (1, 'Ada', 'ada@example.com'), (2, 'Grace', 'grace@example.com')",
    "INSERT INTO wealth.accounts VALUES (10, 1, 100.50)",
    "INSERT INTO wealth.audit_log VALUES (1, 'original')",
    "CREATE VIEW wealth.v_clients_restricted AS SELECT id, name FROM wealth.clients",
    "COMMENT ON TABLE wealth.accounts IS 'One row per client''s account'",
    "COMMENT ON COLUMN wealth.accounts.balance IS 'Current balance in USD'",
    "COMMENT ON VIEW wealth.v_clients_restricted IS 'Clients without contact details'",
    "ALTER TABLE wealth.clients OWNER TO mcp_dev",
    "ALTER TABLE wealth.accounts OWNER TO mcp_dev",
    "ALTER TABLE wealth.audit_log OWNER TO mcp_dev",
    "ALTER VIEW wealth.v_clients_restricted OWNER TO mcp_dev",
    "CREATE ROLE analyst_role",
    "GRANT USAGE ON SCHEMA wealth TO analyst_role",
    "GRANT SELECT ON wealth.v_clients_restricted TO analyst_role",
    f"CREATE ROLE analyst LOGIN PASSWORD '{POSTGRES_PASSWORDS[ANALYST]}'",
    "GRANT analyst_role TO analyst",
]


def mysql_seed(default_role: str) -> list[str]:
    """MySQL and MariaDB: a database is the schema. A role's grants apply only once it is
    the account's default role, which the two engines spell differently (`default_role`)."""
    return [
        "DROP DATABASE IF EXISTS wealth",
        "DROP DATABASE IF EXISTS other_db",
        "DROP USER IF EXISTS analyst",
        "DROP USER IF EXISTS mcp_dev",
        "DROP ROLE IF EXISTS analyst_role",
        "CREATE DATABASE wealth",
        f"CREATE USER mcp_dev IDENTIFIED BY '{MYSQL_PASSWORDS[OWNER]}'",
        "GRANT ALL ON wealth.* TO mcp_dev",
        # Another database the owner can read, which a connection to wealth must not list.
        "CREATE DATABASE other_db",
        "CREATE TABLE other_db.elsewhere (id int PRIMARY KEY)",
        "GRANT SELECT ON other_db.* TO mcp_dev",
        "CREATE TABLE wealth.clients (id int PRIMARY KEY, name varchar(100) NOT NULL, email varchar(200))",
        "CREATE TABLE wealth.accounts (id int PRIMARY KEY AUTO_INCREMENT, client_id int, "
        "balance decimal(12,2) COMMENT 'Current balance in USD', "
        "FOREIGN KEY (client_id) REFERENCES wealth.clients(id)) COMMENT='One row per client''s account'",
        "CREATE TABLE wealth.audit_log (id int PRIMARY KEY, note varchar(100))",
        "INSERT INTO wealth.clients VALUES (1, 'Ada', 'ada@example.com'), (2, 'Grace', 'grace@example.com')",
        "INSERT INTO wealth.accounts VALUES (10, 1, 100.50)",
        "INSERT INTO wealth.audit_log VALUES (1, 'original')",
        "CREATE VIEW wealth.v_clients_restricted AS SELECT id, name FROM wealth.clients",
        "CREATE ROLE analyst_role",
        "GRANT SELECT ON wealth.v_clients_restricted TO analyst_role",
        f"CREATE USER analyst IDENTIFIED BY '{MYSQL_PASSWORDS[ANALYST]}'",
        "GRANT analyst_role TO analyst",
        default_role,
    ]


MYSQL_SEED = mysql_seed("SET DEFAULT ROLE analyst_role TO analyst")
MARIADB_SEED = mysql_seed("SET DEFAULT ROLE analyst_role FOR analyst")
