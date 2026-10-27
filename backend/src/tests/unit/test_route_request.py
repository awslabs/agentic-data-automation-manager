#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""How the HTTP API's requests reach each route, and what a failing route returns.

The HTTP API sends `/api/{proxy+}` and `/api/admin/{proxy+}` as the route key, so
the function itself matches the real path against its route table. Each test uses
a service's own route table, with recorders in place of the route functions.
"""

import json

import pytest

from shared.http_response import UnauthenticatedError, route_request

ARN = "arn:aws:dynamodb:us-east-1:123456789012:table/secret-table"


def _event(method: str, path: str, proxy: str) -> dict:
    """As API Gateway v2 sends it for a request on a {proxy+} route."""
    return {"routeKey": f"{method} {proxy}", "headers": {},
            "requestContext": {"http": {"method": method, "path": path}}}


def _recording(routes: dict) -> tuple[dict, list]:
    calls = []
    return {key: (lambda key: lambda event, path, query, body: calls.append((key, path)) or {
        "statusCode": 200, "headers": {}, "body": "{}"})(key) for key in routes}, calls


def _routes(module: str) -> dict:
    import importlib
    return importlib.import_module(module).ROUTES


class TestProxyRoutesReachTheirFunction:

    @pytest.mark.parametrize("method, path, proxy, route, params", [
        ("PATCH", "/api/admin/scan-targets/t-1", "/api/admin/{proxy+}",
         "PATCH /api/admin/scan-targets/{targetId}", {"targetId": "t-1"}),
        ("DELETE", "/api/admin/scan-targets/t-1", "/api/admin/{proxy+}",
         "DELETE /api/admin/scan-targets/{targetId}", {"targetId": "t-1"}),
    ])
    def test_admin(self, method, path, proxy, route, params):
        routes, calls = _recording(_routes("adam.schema_provider.admin_handler"))
        assert route_request(_event(method, path, proxy), routes)["statusCode"] == 200
        assert calls == [(route, params)]

    @pytest.mark.parametrize("method", ["PATCH", "DELETE"])
    def test_connections(self, method):
        routes, calls = _recording(_routes("adam.data_source_provider.connections_api_handler"))
        event = _event(method, "/api/connections/c-1", "/api/connections/{proxy+}")
        assert route_request(event, routes)["statusCode"] == 200
        assert calls == [(f"{method} /api/connections/{{connectionId}}", {"connectionId": "c-1"})]

    def test_a_path_with_no_route_is_404(self):
        routes, calls = _recording(_routes("adam.schema_provider.admin_handler"))
        response = route_request(_event("PATCH", "/api/admin/scan-targets/t-1/extra", "/api/admin/{proxy+}"), routes)
        assert response["statusCode"] == 404 and calls == []


class TestAFailingRouteIsMapped:

    def _raising(self, error):
        def route(event, path, query, body):
            raise error
        return {"GET /api/thing": route}

    def test_an_unattributable_request_is_401(self):
        response = route_request({"routeKey": "GET /api/thing", "headers": {}},
                                 self._raising(UnauthenticatedError("no sub")))
        assert response["statusCode"] == 401

    def test_any_other_error_is_500_with_only_a_reference(self):
        """The error's own text can name tables, settings and resources: it stays in the log."""
        response = route_request({"routeKey": "GET /api/thing", "headers": {},
                                  "requestContext": {"requestId": "req-123"}},
                                 self._raising(RuntimeError(f"AccessDenied on {ARN}: table sessions-x")))
        assert response["statusCode"] == 500
        assert json.loads(response["body"]) == {"error": "Internal error. Reference: req-123"}
