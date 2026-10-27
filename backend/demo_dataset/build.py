#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Load the AnyCompany demo dataset into both databases, then verify it there.

DESTRUCTIVE with --apply:
  * Oracle MCP_DEV: drops and recreates the banking tables and views from
    oracle_schema.sql (grants included), then loads them. If the schema owner is
    READ ONLY -- the state an account must be in to be registered -- the loader
    lifts it for the write and puts it back, which takes an admin credential.
  * Aurora wealth: drops and recreates the wealth-platform schema from
    aurora_schema.sql, grants included, and loads it.
Take a backup first. Each database is loaded in a single transaction where the
engine allows it.

Without --apply it generates the data and checks every invariant in memory, and
touches nothing.

It talks to two databases this machine can already reach (for private ones, open
a port-forward first). From backend/:

    python -m demo_dataset.build \\
        --profile <profile> --region <region> \\
        --oracle-secret-arn <arn> --oracle-admin-secret-arn <arn> --oracle-dsn <host:port/service> \\
        --aurora-secret-arn <arn> --aurora-host <host> --aurora-port 5432 \\
        --aurora-database <db> [--apply]
"""

import argparse
import contextlib
import csv
import io
import json
import re
import ssl
import sys
from pathlib import Path

import boto3
import oracledb
import pg8000.native

from demo_dataset import generate as g

HERE = Path(__file__).parent

ORACLE_TABLES = ["RELATIONSHIP_MANAGERS", "CLIENTS", "CLIENT_LIFE_EVENTS", "ACCOUNTS",
                 "TRANSACTIONS", "CLIENT_CONTACTS"]
AURORA_TABLES = ["securities", "portfolio_goals", "investment_accounts", "positions", "trades",
                 "advisory_activity"]
SERIAL_KEYS = {"portfolio_goals": "goal_id", "investment_accounts": "account_id",
               "positions": "position_id", "trades": "trade_id",
               "advisory_activity": "activity_id"}

# Invariants re-checked in the loaded databases. Each query returns the number of
# violating rows; anything but 0 is a failure.
ORACLE_CHECKS = {
    "contact logged by another client's RM": """
        SELECT COUNT(*) FROM client_contacts x JOIN clients c ON c.client_id = x.client_id
        WHERE x.rm_id <> c.relationship_manager_id""",
    "running balance does not follow from amount": """
        SELECT COUNT(*) FROM (
          SELECT running_balance,
                 LAG(running_balance) OVER (PARTITION BY account_id ORDER BY txn_date, txn_id) prev,
                 amount FROM transactions)
        WHERE prev IS NOT NULL AND prev + amount <> running_balance""",
    "balance != latest running balance": """
        SELECT COUNT(*) FROM accounts a JOIN (
          SELECT account_id, running_balance,
                 ROW_NUMBER() OVER (PARTITION BY account_id ORDER BY txn_date DESC, txn_id DESC) rn
          FROM transactions) t ON t.account_id = a.account_id AND t.rn = 1
        WHERE t.running_balance <> a.balance""",
    "running balance is NULL": "SELECT COUNT(*) FROM transactions WHERE running_balance IS NULL",
    "life event recorded by another client's RM": """
        SELECT COUNT(*) FROM client_life_events e JOIN clients c ON c.client_id = e.client_id
        WHERE e.rm_id <> c.relationship_manager_id""",
}
AURORA_CHECKS = {
    "position quantity != net BUY - SELL trades": """
        SELECT COUNT(*) FROM positions p LEFT JOIN (
          SELECT account_id, symbol,
                 SUM(CASE trade_type WHEN 'BUY' THEN quantity WHEN 'SELL' THEN -quantity ELSE 0 END) net
          FROM trades GROUP BY 1, 2) t USING (account_id, symbol)
        WHERE t.net IS DISTINCT FROM p.quantity""",
    "traded but not held": """
        SELECT COUNT(*) FROM (SELECT DISTINCT account_id, symbol FROM trades) t
        LEFT JOIN positions p USING (account_id, symbol) WHERE p.position_id IS NULL""",
    "goal with no funding account": """
        SELECT COUNT(*) FROM portfolio_goals g
        WHERE NOT EXISTS (SELECT 1 FROM investment_accounts a WHERE a.goal_id = g.goal_id)""",
    "goal funded by another client's account": """
        SELECT COUNT(*) FROM investment_accounts a JOIN portfolio_goals g USING (goal_id)
        WHERE a.client_id <> g.client_id""",
    "retirement funded by a non-retirement account": """
        SELECT COUNT(*) FROM investment_accounts a JOIN portfolio_goals g USING (goal_id)
        WHERE g.goal_type = 'RETIREMENT' AND a.account_type NOT IN ('401K','IRA','ROTH_IRA')""",
}


def _secret(session, arn: str) -> dict:
    return json.loads(session.client("secretsmanager").get_secret_value(SecretId=arn)["SecretString"])


def _oracle_rows(table, rows):
    cols = list(rows[0])
    return cols, [tuple(r[c] for c in cols) for r in rows]


ORACLE_VIEWS = ["V_CLIENTS_RESTRICTED"]
_IDENTIFIER = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def _quoted(name: str) -> str:
    """A table or column name, checked and quoted: the names are this module's and generate.py's
    own, but a name that is not a plain identifier stops the load instead of reaching SQL."""
    if not _IDENTIFIER.match(name):
        raise ValueError(f"Not a plain identifier: {name!r}")
    return f'"{name}"'


RDS_CA_BUNDLE = (HERE.parent / "src" / "adam" / "data_source_provider" / "certs"
                 / "rds-global-bundle.pem")


@contextlib.contextmanager
def oracle_writable(admin_conn, owner: str):
    """Let `owner` write for the duration of the block, then put it back as it was.

    A READ ONLY account is the state registration requires, so the schema owner
    usually is one. The restriction is restored in `finally`, so a failed load
    does not leave the account writable. Only an account that was READ ONLY is
    touched.
    """
    if not _IDENTIFIER.match(owner):
        raise ValueError(f"{owner!r} is not a plain identifier; refusing to build SQL from it")
    cur = admin_conn.cursor()
    cur.execute("SELECT read_only FROM dba_users WHERE username = :u", u=owner.upper())
    row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"Oracle user {owner} does not exist")
    was_read_only = row[0] == "YES"
    if was_read_only:
        cur.execute(f"ALTER USER {owner} READ WRITE")
    try:
        yield was_read_only
    finally:
        if was_read_only:
            cur.execute(f"ALTER USER {owner} READ ONLY")


def create_oracle_schema(conn) -> None:
    """Drop the dataset's own tables and views, then create them from oracle_schema.sql."""
    cur = conn.cursor()
    drops = [f"DROP VIEW {v}" for v in ORACLE_VIEWS] + \
            [f"DROP TABLE {t} CASCADE CONSTRAINTS PURGE" for t in reversed(ORACLE_TABLES)]
    for drop in drops:
        try:
            cur.execute(drop)
        except Exception as e:
            if "ORA-00942" not in str(e):      # not there yet is fine
                raise
    for stmt in _statements((HERE / "oracle_schema.sql").read_text()):
        cur.execute(stmt)


def tunnel_ssl_context() -> ssl.SSLContext:
    """Verify the server's certificate chain against the RDS bundle.

    The hostname is not checked: reached through a port-forward the connection
    is to localhost, which the certificate cannot name. The chain still has to
    validate, which is more than pg8000's `ssl_context=True` does.
    """
    context = ssl.create_default_context(cafile=str(RDS_CA_BUNDLE))
    context.check_hostname = False
    return context


def load_oracle(conn, data) -> None:
    cur = conn.cursor()
    for table in reversed(ORACLE_TABLES):          # children first
        cur.execute(f"DELETE FROM {_quoted(table)}")  # noqa: S608  # nosec B608 - identifiers checked and quoted
    for table in ORACLE_TABLES:                    # parents first
        cols, rows = _oracle_rows(table, data[table])
        binds = ", ".join(f":{i + 1}" for i in range(len(cols)))
        names = ", ".join(_quoted(c) for c in cols)
        sql = f"INSERT INTO {_quoted(table)} ({names}) VALUES ({binds})"  # noqa: S608  # nosec B608 - identifiers quoted
        for i in range(0, len(rows), 10_000):
            cur.executemany(sql, rows[i:i + 10_000])
        print(f"  oracle   {table:24} {len(rows):>7}")


def _statements(sql: str) -> list[str]:
    body = "\n".join(line for line in sql.splitlines() if not line.lstrip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def load_aurora(conn, data) -> None:
    for stmt in _statements((HERE / "aurora_schema.sql").read_text()):
        conn.run(stmt)
    for table in AURORA_TABLES:
        rows = data[table]
        cols = list(rows[0])
        buf = io.StringIO()
        w = csv.writer(buf)
        for r in rows:
            w.writerow(["" if r[c] is None else r[c] for c in cols])
        buf.seek(0)
        names = ", ".join(_quoted(c) for c in cols)
        conn.run(f"COPY {_quoted(table)} ({names}) FROM STDIN WITH (FORMAT csv, NULL '')", stream=buf)
        if table in SERIAL_KEYS:
            key = SERIAL_KEYS[table]
            # The sequence is found by bound names; MAX() needs them as identifiers.
            conn.run(f"SELECT setval(pg_get_serial_sequence(:table, :key), (SELECT MAX({_quoted(key)}) "  # noqa: S608  # nosec B608
                     f"FROM {_quoted(table)}))", table=table, key=key)
        print(f"  aurora   {table:24} {len(rows):>7}")


def verify(ora, pg) -> list[str]:
    failures = []
    cur = ora.cursor()
    for name, sql in ORACLE_CHECKS.items():
        cur.execute(sql)
        if (n := cur.fetchone()[0]):
            failures.append(f"oracle: {name}: {n}")
    for name, sql in AURORA_CHECKS.items():
        if (n := pg.run(sql)[0][0]):
            failures.append(f"aurora: {name}: {n}")
    # Across engines: no foreign key can say this, so it is checked here.
    cur.execute("SELECT client_id, relationship_manager_id FROM clients")
    rm_of = dict(cur.fetchall())
    for (cid,) in pg.run("SELECT DISTINCT client_id FROM investment_accounts UNION "
                         "SELECT DISTINCT client_id FROM portfolio_goals UNION "
                         "SELECT DISTINCT client_id FROM advisory_activity"):
        if cid not in rm_of:
            failures.append(f"cross: client {cid} in Aurora but not in Oracle")
    wrong = sum(1 for cid, rm in pg.run("SELECT client_id, rm_id FROM advisory_activity") if rm_of.get(cid) != rm)
    if wrong:
        failures.append(f"cross: advisory activity by another client's RM: {wrong}")
    # A planned retirement is the retirement goal's target date, across engines.
    cur.execute("SELECT client_id, event_date FROM client_life_events WHERE event_type = 'PLANNED_RETIREMENT'")
    planned = {cid: d.date() for cid, d in cur.fetchall()}
    goals = dict(pg.run("SELECT client_id, target_date FROM portfolio_goals WHERE goal_type = 'RETIREMENT'"))
    if planned != goals:
        failures.append(f"cross: planned retirement != retirement goal date for "
                        f"{len(set(planned.items()) ^ set(goals.items()))} clients")
    return failures


def run(*, oracle: dict, aurora: dict, oracle_admin: dict | None = None, apply: bool) -> int:
    """Generate, check, and (with apply) load both databases, then verify them.

    oracle and oracle_admin hold user, password and dsn; aurora holds user,
    password, host, port, database and ssl_context (None for no TLS).
    """
    data = g.generate()
    failures = g.check(data)
    print(json.dumps(g.story(data), indent=2, default=str))
    if failures:
        print(f"\n{len(failures)} invariant failures in generated data; nothing loaded:")
        for f in failures[:20]:
            print("  ", f)
        return 1
    print("\ngenerated data: all invariants hold")
    if not apply:
        print("dry run: nothing written (pass --apply to load)")
        return 0

    pg = pg8000.native.Connection(user=aurora["user"], password=aurora["password"],
                                  host=aurora["host"], port=aurora["port"],
                                  database=aurora["database"], ssl_context=aurora["ssl_context"])
    admin = oracledb.connect(**oracle_admin) if oracle_admin else None
    ora = None
    try:
        print("\nloading")
        with oracle_writable(admin, oracle["user"]) if admin else contextlib.nullcontext():
            # Opened after the restriction is lifted: a session established while the
            # account is READ ONLY stays read-only.
            ora = oracledb.connect(**oracle)
            create_oracle_schema(ora)
            load_oracle(ora, data)
            ora.commit()
            ora.close()
            ora = oracledb.connect(**oracle)
        pg.run("BEGIN")
        try:
            load_aurora(pg, data)
            pg.run("COMMIT")
        except Exception:
            pg.run("ROLLBACK")
            raise
        failures = verify(ora, pg)
    except Exception:
        if ora is not None:
            ora.rollback()
        raise
    finally:
        if ora is not None:
            ora.close()
        if admin is not None:
            admin.close()
        pg.close()
    if failures:
        print(f"\n{len(failures)} invariant failures in the loaded databases:")
        for f in failures:
            print("  ", f)
        return 1
    print("\nloaded data: all invariants hold in both databases")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    for flag in ("--profile", "--region", "--oracle-secret-arn", "--oracle-dsn",
                 "--aurora-secret-arn", "--aurora-host", "--aurora-database"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--oracle-admin-secret-arn",
                    help="needed to lift READ ONLY on the schema owner for the write")
    ap.add_argument("--aurora-port", type=int, required=True)
    ap.add_argument("--apply", action="store_true", help="write to both databases (destructive)")
    args = ap.parse_args()

    if not args.apply:
        return run(oracle={}, aurora={}, apply=False)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    o, a = _secret(session, args.oracle_secret_arn), _secret(session, args.aurora_secret_arn)
    admin = _secret(session, args.oracle_admin_secret_arn) if args.oracle_admin_secret_arn else None
    return run(
        oracle={"user": o["username"], "password": o["password"], "dsn": args.oracle_dsn},
        oracle_admin=({"user": admin["username"], "password": admin["password"], "dsn": args.oracle_dsn}
                      if admin else None),
        aurora={"user": a["username"], "password": a["password"], "host": args.aurora_host,
                "port": args.aurora_port, "database": args.aurora_database,
                "ssl_context": tunnel_ssl_context()},
        apply=True,
    )


if __name__ == "__main__":
    sys.exit(main())
