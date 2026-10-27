#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A connection is opened where its database version is registered, never where a copy says.

The real connection_opening.registered, with only the Lambda client faked: it stands for
the schema service's function (the integration tests call the real one).
"""

import io
import json

import pytest

from adam.data_source_provider import connection_opening
from adam.data_source_provider.connection_registry import ConnectionError_
from adam.shared import provider_client

REGISTERED = {"targetId": "t1", "name": "Banking", "engine": "oracle", "host": "db.new", "port": "2484",
              "database": "ORCL", "tls": True, "tlsCertificate": "-----BEGIN CERTIFICATE-----",
              "statementTimeoutSeconds": 30, "rowCap": 1000}


class _SchemaService:
    def __init__(self, answer):
        self.answer, self.asked = answer, []

    def invoke(self, FunctionName, InvocationType, Payload):
        self.asked.append((FunctionName, json.loads(Payload)))
        return {"Payload": io.BytesIO(json.dumps(self.answer).encode())}


@pytest.fixture
def schema(monkeypatch):
    monkeypatch.setenv("SCHEMA_PROVIDER_FUNCTION_NAME", "schema-fn")

    def answering(answer):
        service = _SchemaService(answer)
        monkeypatch.setattr(provider_client, "_lambda_client", service)
        return service
    return answering


def test_the_registration_wins_over_whatever_an_old_row_still_holds(schema):
    schema({"response": REGISTERED})
    old_row = {"userId": "u", "connectionId": "c", "targetId": "t1", "secretName": "/s",
               "engine": "postgres", "host": "db.old", "port": "1521", "database": "X", "dsn": "db.old:1521/X"}
    connection = connection_opening.registered(old_row)
    assert {k: connection[k] for k in ("engine", "host", "port", "database", "dsn", "tls", "tlsCertificate")} == {
        "engine": "oracle", "host": "db.new", "port": "2484", "database": "ORCL", "dsn": "db.new:2484/ORCL",
        "tls": True, "tlsCertificate": "-----BEGIN CERTIFICATE-----"}
    assert connection["secretName"] == "/s"  # the login is still the user's own


def test_the_registration_is_asked_for_the_rows_own_database(schema):
    service = schema({"response": REGISTERED})
    connection_opening.registered({"userId": "u", "connectionId": "c", "targetId": "t1", "secretName": "/s"})
    assert service.asked == [("schema-fn", {"operation": "get_database", "args": {"targetId": "t1"}})]


@pytest.mark.parametrize("refusal", ["That database is not registered.",
                                     "Banking was updated by an administrator. Remove this connection and connect "
                                     "to Banking again with your login."])
def test_a_version_the_schema_service_refuses_is_refused_with_its_reason(schema, refusal):
    """The schema service writes why for the user: not registered, or disabled and whether replaced."""
    schema({"error": refusal})
    with pytest.raises(ConnectionError_) as refused:
        connection_opening.registered({"userId": "u", "connectionId": "c", "targetId": "t1", "secretName": "/s"})
    assert str(refused.value) == refusal


def test_a_row_for_no_registered_database_is_refused_without_asking(schema):
    service = schema({"response": REGISTERED})
    with pytest.raises(ConnectionError_, match="Remove it and add it again"):
        connection_opening.registered({"userId": "u", "connectionId": "c", "secretName": "/s", "host": "db.old"})
    assert service.asked == []
