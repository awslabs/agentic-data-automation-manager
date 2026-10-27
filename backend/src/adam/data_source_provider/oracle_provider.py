#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Oracle DataSourceProvider — queries Oracle Database via python-oracledb.

Logs in with the Credential it is given. Scans ALL_TAB_COLUMNS and
ALL_CONSTRAINTS to generate rich CREATE TABLE DDL including primary keys,
foreign keys, unique constraints, and check constraints.
"""

import hashlib
import logging
import ssl
from datetime import datetime
from typing import Any

import oracledb

from adam.data_source_provider.data_source_provider import (
    Column,
    DataSourceProvider,
    comment_ddl,
    create_table_ddl,
    failure_response,
    rows_response,
)
from adam.data_source_provider.tls import verified_ssl_context

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# Rows read at a time while skipping to start_row.
_SKIP_BATCH = 5000


_UNREADABLE = " Its certificate could not be read: "
_TLS_OFF = "TLS is turned off for this function (DB_TLS_DISABLED, for local test containers only)."


def _tls_reason(reason: str) -> str:
    """Why a TLS connection to the database failed, in the administrator's terms."""
    if reason == _TLS_OFF:
        return f"TLS was not tried: {reason}"
    if "CERTIFICATE_VERIFY_FAILED" in reason:
        unreadable = reason.partition(_UNREADABLE)[2]
        return ("It answered TLS on this port, but its certificate is not signed by a CA in the bundle this "
                "service verifies against (DB_TLS_CA_FILE)"
                + (f", and its own certificate could not be read to trust it instead ({unreadable})." if unreadable
                   else "."))
    if "DPY-6006" in reason:
        return "It answered TLS on this port, but its certificate does not name the host registered for it."
    return f"It did not answer TLS on this port ({reason})."


def _pinned_context(certificate: str) -> ssl.SSLContext:
    """Trust exactly this certificate, the database's own. The driver checks that what the
    server presents names what this certificate names (ssl_server_cert_dn), wherever a SCAN
    listener hands the client on to; a different key is refused whatever it names.

    It is often self-signed X.509 version 1, which strict verification refuses even as a trust
    anchor, so only that check is relaxed. The default context already lets one certificate
    anchor alone, as a leaf from a CA the service does not carry needs."""
    context = ssl.create_default_context(cadata=certificate)
    context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


def _subject(certificate: str) -> str:
    """The certificate's subject DN, as the driver's ssl_server_cert_dn compares it."""
    from cryptography import x509
    return x509.load_pem_x509_certificate(certificate.encode()).subject.rfc4514_string()


def _fingerprint(certificate: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate)).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))


def _no_tls_warning(reason: str) -> str:
    """What the administrator is told when a database is reached without TLS."""
    return (f"TLS is not enabled on this database, so its connections are not encrypted. {_tls_reason(reason)} "
            "If it has a TLS (TCPS) listener on another port, often 2484, register that port instead.")

class OracleProvider(DataSourceProvider):
    """Database provider backed by Oracle via python-oracledb."""

    PROBE_STATEMENT = "SELECT 1 FROM DUAL"

    STATEMENT_BEST_PRACTICES = """
1. Use the search_schema tool with the user's question to find relevant tables.
2. Oracle has no LIMIT clause. Limit rows with FETCH FIRST n ROWS ONLY, never LIMIT n or TOP n.
3. Use UPPER() or LOWER() on both sides of string equality to avoid case mismatches.
4. Before guessing values, run exploratory queries (FETCH FIRST 1 ROW ONLY, SELECT DISTINCT).
5. Generate SELECT statements, run them with execute_statement, and retrieve results.
6. If no results, simplify by removing a WHERE clause or selecting distinct values from that column.
7. Use Oracle features: analytical functions, NVL, CONNECT BY. A SELECT with no table needs FROM DUAL.
8. Only execute SELECT statements. Never modify data.
"""

    # SET TRANSACTION READ ONLY blocks DML but not DDL: a DROP TABLE ends the
    # transaction and then runs. Hence the account-level steps for the DBA.
    ADMIN_BEST_PRACTICES = """
User accounts, one per person or role:
1. Grant CREATE SESSION, and SELECT or READ only on the tables and views that person should see. Grant no ANY or \
CREATE privileges, directly or through a role, and no EXECUTE on a package, procedure or function that runs with its \
owner's rights (AUTHID DEFINER, the default): it can write with the owner's privileges, whatever this account may do. \
That includes what PUBLIC is granted.
2. Don't register a schema owner: an owner can alter and drop its own objects.
3. Every statement runs in a read-only transaction, which blocks DML but not DDL. To block DDL:
   - Oracle AI Database 26ai (release 23.26) and later: ALTER USER <username> READ ONLY;
     The account can't switch this off.
   - Earlier releases: as an administrator, create a trigger that refuses DDL from these accounts:
     CREATE OR REPLACE TRIGGER agent_accounts_no_ddl
     BEFORE DDL ON DATABASE
     BEGIN
       IF ora_login_user IN ('<USERNAME_1>', '<USERNAME_2>') THEN
         RAISE_APPLICATION_ERROR(-20001, 'DDL is not allowed for this account.');
       END IF;
     END;
     /

Scanner account, one per database:
4. Grant CREATE SESSION, and REFERENCES on each table and view to index. REFERENCES makes an object's columns, \
constraints and comments visible in the ALL_* views without allowing queries on its rows.
5. Block DDL for it as in step 3.
"""

    @property
    def conn(self) -> oracledb.Connection:
        """The session, opened on first use as the credential this provider was given.

        Over TLS (tcps, the certificate and hostname verified) when the registered
        database's `tls` says so, which the probe decided when an administrator saved it.
        """
        if self._db is None:
            dsn = self._row.get("dsn", "")
            if not dsn:
                raise ValueError(f"Oracle connection {self._row.get('connectionId', '?')} has no dsn.")
            tls = self._row.get("tls")
            if not isinstance(tls, bool):
                raise ValueError(
                    f"Oracle connection {self._row.get('connectionId', '?')} has no TLS setting. An administrator "
                    "saves its database on the Admin page, which checks whether the database offers TLS.")
            names = {}
            if tls and self._row.get("tlsCertificate"):
                context = _pinned_context(self._row["tlsCertificate"])
                # A SCAN listener hands the client on to a node by its address, which the pinned
                # certificate does not name; the driver checks the certificate names what the
                # pinned one names instead.
                names = {"ssl_server_dn_match": True, "ssl_server_cert_dn": _subject(self._row["tlsCertificate"])}
            elif tls:
                context = verified_ssl_context()
                if context is None:
                    raise RuntimeError(_TLS_OFF)
            if tls:
                self._db = oracledb.connect(user=self._credential.username, password=self._credential.password,
                                            dsn=f"tcps://{dsn}", ssl_context=context, **names)
            else:
                self._db = oracledb.connect(
                    user=self._credential.username, password=self._credential.password, dsn=dsn)
            logger.info("Connected to Oracle at %s (%s) as database user %s for subject %s",
                        dsn, "TLS" if tls else "no TLS", self._credential.username, self._subject)
        return self._db

    def probe(self) -> dict[str, Any]:
        """Log in. Asked to discover TLS (the admin saving a database, through its scanner),
        try TLS first and report which was used, with the reason when it was not."""
        if self._row.get("discoverTls") is not True:
            self._select_one()
            return {"ok": True}
        self._row["tls"] = True
        try:
            self._select_one()
            return {"ok": True, "tls": True}
        except RuntimeError as e:
            # An ORA- error came from the database, over TLS: the login is wrong, not the transport.
            if str(e).startswith("ORA-"):
                raise
            reason = str(e)
        if "CERTIFICATE_VERIFY_FAILED" in reason:
            # It speaks TLS with a certificate no CA in the bundle signed, often its own. Trust
            # the one it presents now, only it, and keep it: the admin checks the fingerprint.
            self.close()
            try:
                certificate = self._presented_certificate()
            except OSError as e:  # ssl.SSLError is one
                reason = f"{reason}{_UNREADABLE}{e}"
            else:
                self._row["tlsCertificate"] = certificate
                try:
                    self._select_one()
                    return {"ok": True, "tls": True, "tlsCertificate": certificate,
                            "tlsFingerprint": _fingerprint(certificate)}
                except RuntimeError as e:
                    if str(e).startswith("ORA-"):
                        raise
                    reason = str(e)
                del self._row["tlsCertificate"]
        self.close()
        self._row["tls"] = False
        try:
            self._select_one()
        except RuntimeError as e:
            raise RuntimeError(f"Over TLS: {_tls_reason(reason)} Without TLS: {e}") from e
        return {"ok": True, "tls": False, "tlsWarning": _no_tls_warning(reason)}

    def _presented_certificate(self) -> str:
        """The certificate the database's TLS listener presents, unverified: what it is is the question."""
        host, _, rest = self._row["dsn"].partition(":")
        return ssl.get_server_certificate((host, int(rest.partition("/")[0])), timeout=self.INTERNAL_TIMEOUT_SECONDS)

    def _select_one(self) -> None:
        super().probe()

    def visible_objects(self) -> list[tuple[str, str, str]]:
        # ALL_TABLES and ALL_VIEWS already list exactly what the logged-in user can
        # access, including through roles. Oracle-maintained schemas (SYS, SYSTEM, ...)
        # are excluded because they are the dictionary itself, not data anyone asked about.
        return self._listed_by("""
            SELECT owner AS schema_name, table_name AS object_name, 'table' AS kind FROM all_tables
            WHERE dropped = 'NO'
            AND owner NOT IN (SELECT username FROM all_users WHERE oracle_maintained = 'Y')
            UNION
            SELECT owner, view_name, 'view' FROM all_views
            WHERE owner NOT IN (SELECT username FROM all_users WHERE oracle_maintained = 'Y')
            ORDER BY 1, 2
        """)

    def execute_statement(self, statement: str, *, timeout_seconds: int, row_cap: int,
                             start_row: int = 0) -> dict[str, Any]:
        statement = statement.strip().rstrip(";")
        try:
            conn = self.conn
            conn.call_timeout = timeout_seconds * 1000
            try:
                with conn.cursor() as cursor:
                    # Must be the first statement of a transaction; the rollback
                    # below ends it, so every call starts a fresh one.
                    cursor.execute("SET TRANSACTION READ ONLY")
                    cursor.execute(statement)
                    columns = [desc[0] for desc in cursor.description] if cursor.description else []
                    # Wrapping the statement in OFFSET ... FETCH would break duplicate
                    # column names in a join, so earlier pages are read and dropped.
                    skipped = 0
                    while skipped < start_row:
                        batch = cursor.fetchmany(min(_SKIP_BATCH, start_row - skipped))
                        if not batch:
                            break
                        skipped += len(batch)
                    fetched = cursor.fetchmany(row_cap + 1)
            finally:
                conn.rollback()

            values = [[v.isoformat() if isinstance(v, datetime) else v for v in row] for row in fetched[:row_cap]]
            return rows_response(columns, values, len(fetched) > row_cap, start_row)
        except oracledb.Error as e:
            return failure_response(e)

    def object_ddl(self, schema: str, name: str, kind: str) -> str:
        table = name
        ddl = (self._view_ddl(schema=schema, view=table) if kind == "view"
               else create_table_ddl(kind, schema, table, self._columns(schema, table),
                                     self._get_constraints_info(schema, table)))
        # Oracle comments on a view with COMMENT ON TABLE too.
        comments = comment_ddl("TABLE", f"{schema}.{table}", *self._comments(schema, table))
        return f"{ddl}\n{comments}" if comments else ddl

    def _comments(self, schema: str, table: str) -> tuple[str | None, list[tuple[str, str]]]:
        """The table's comment and each commented column's, in column order."""
        with self.conn.cursor() as cursor:
            cursor.execute(
                "SELECT comments FROM all_tab_comments "
                "WHERE owner = UPPER(:schema_name) AND table_name = UPPER(:table_name)",
                schema_name=schema, table_name=table)
            row = cursor.fetchone()
            cursor.execute("""
                SELECT c.column_name, c.comments
                FROM all_col_comments c
                JOIN all_tab_columns t ON t.owner = c.owner AND t.table_name = c.table_name
                    AND t.column_name = c.column_name
                WHERE c.owner = UPPER(:schema_name) AND c.table_name = UPPER(:table_name)
                AND c.comments IS NOT NULL
                ORDER BY t.column_id""", schema_name=schema, table_name=table)
            columns = cursor.fetchall()
        return (row[0] if row else None), [(name, text) for name, text in columns]

    def _view_ddl(self, *, schema: str, view: str) -> str:
        """A view's own definition, not a column list.

        The query text is what carries the meaning: a restricted view's value to
        the agent is which columns and rows it leaves out, and a bare column
        list says nothing about that. It is also the only rendering that is
        valid SQL -- `CREATE VIEW x (cols);` is not.
        """
        with self.conn.cursor() as cursor:
            # LONG, so it has to be fetched as a string rather than streamed.
            cursor.outputtypehandler = lambda c, *a: c.var(oracledb.DB_TYPE_LONG, arraysize=c.arraysize)
            # Bind named view_name, not :view -- VIEW is reserved, and Oracle
            # rejects it as a bind name with ORA-01745.
            cursor.execute(
                "SELECT text FROM all_views WHERE owner = :owner AND view_name = :view_name",
                owner=schema, view_name=view,
            )
            row = cursor.fetchone()
        if not row or not row[0]:
            raise ValueError(
                f"No definition for view {schema}.{view}; it does not exist or "
                "this connection cannot see it."
            )
        return f"CREATE VIEW {schema}.{view} AS\n{row[0].strip()};"

    def _get_constraints_info(self, schema_name: str, table_name: str) -> dict:
        info = {"primary_keys": {}, "foreign_keys": {}, "unique_constraints": {}, "check_constraints": {}}

        query = """
        SELECT constraint_name, constraint_type, search_condition, r_owner, r_constraint_name
        FROM all_constraints
        WHERE owner = UPPER(:schema_name)
        AND table_name = UPPER(:table_name)
        AND constraint_type IN ('P', 'R', 'U', 'C')
        ORDER BY constraint_type, constraint_name
        """
        with self.conn.cursor() as cursor:
            cursor.execute(query, schema_name=schema_name, table_name=table_name)
            rows = cursor.fetchall()

        for constraint_name, ctype, search_condition, r_owner, r_constraint_name in rows:
            if ctype in ("P", "U"):
                kind = "primary_keys" if ctype == "P" else "unique_constraints"
                info[kind][constraint_name] = self._get_constraint_columns(schema_name, constraint_name)
            elif ctype == "R":
                fk_cols = self._get_constraint_columns(schema_name, constraint_name)
                ref_cols = self._get_constraint_columns(r_owner, r_constraint_name)
                ref_table = self._get_constraint_table(r_owner, r_constraint_name)
                info["foreign_keys"][constraint_name] = {
                    "columns": fk_cols,
                    "ref_schema": r_owner,
                    "ref_table": ref_table,
                    "ref_columns": ref_cols,
                }
            elif ctype == "C":
                if search_condition and not search_condition.strip().endswith("IS NOT NULL"):
                    info["check_constraints"][constraint_name] = search_condition

        return info

    def _get_constraint_columns(self, owner: str, constraint_name: str) -> list[str]:
        query = """
        SELECT column_name FROM all_cons_columns
        WHERE owner = UPPER(:owner) AND constraint_name = :constraint_name
        ORDER BY position
        """
        with self.conn.cursor() as cursor:
            cursor.execute(query, owner=owner, constraint_name=constraint_name)
            return [row[0] for row in cursor.fetchall()]

    def _get_constraint_table(self, owner: str, constraint_name: str) -> str:
        query = """
        SELECT table_name FROM all_constraints
        WHERE owner = UPPER(:owner) AND constraint_name = :constraint_name
        """
        with self.conn.cursor() as cursor:
            cursor.execute(query, owner=owner, constraint_name=constraint_name)
            row = cursor.fetchone()
            return row[0] if row else ""

    def _columns(self, schema: str, table: str) -> list[Column]:
        with self.conn.cursor() as cursor:
            cursor.execute("""
            SELECT column_name, data_type, data_length, data_precision, data_scale, nullable, data_default
            FROM all_tab_columns
            WHERE owner = UPPER(:schema_name) AND table_name = UPPER(:table_name)
            ORDER BY column_id
            """, schema_name=schema, table_name=table)
            rows = cursor.fetchall()
        return [Column(name, self._format_data_type(data_type, data_length, data_precision, data_scale),
                       nullable == "N", data_default)
                for name, data_type, data_length, data_precision, data_scale, nullable, data_default in rows]

    @staticmethod
    def _format_data_type(data_type: str, data_length: int, data_precision: int, data_scale: int) -> str:
        if data_type == "VARCHAR2":
            return f"VARCHAR2({data_length})"
        elif data_type == "NUMBER" and data_precision is not None:
            if data_scale is not None and data_scale > 0:
                return f"NUMBER({data_precision},{data_scale})"
            return f"NUMBER({data_precision})"
        elif data_type == "CHAR":
            return f"CHAR({data_length})"
        return data_type

