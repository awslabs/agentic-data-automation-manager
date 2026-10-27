#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CORS is decided in the Lambda, from the origin_* parameters under /{stack}/.

Real route_request and real SSM (moto). The API Gateway has no CORS layer of its own,
so what these tests assert is what a browser gets. The allowlist is every parameter
named origin_*: the Frontend stack publishes origin_frontend, and a developer who wants
to call a deployed stack from a local dev server adds origin_frontend_local by hand.
"""

import boto3
import pytest

from shared import http_response

FRONTEND = "d111111abcdef8.cloudfront.net"
LOCAL = "http://localhost:5173"


@pytest.fixture
def cors(registry, monkeypatch):
    """Point the Lambda code at moto, with a clean cache and the frontend origin published."""
    ssm = boto3.client("ssm")
    stack = "/corstest"
    monkeypatch.setattr(http_response, "_stack_name", "corstest")
    monkeypatch.setattr(http_response, "_allowed_origins", None)
    monkeypatch.setattr(http_response, "_ssm_client", None)
    created = []

    def put(name, value):
        ssm.put_parameter(Name=f"{stack}/{name}", Value=value, Type="String", Overwrite=True)
        created.append(f"{stack}/{name}")
    put("origin_frontend", FRONTEND)

    def fresh_container():
        """A new Lambda container reads the allowlist again; the old one kept its cache."""
        monkeypatch.setattr(http_response, "_allowed_origins", None)
    yield put, fresh_container, ssm
    for name in created:
        try:
            ssm.delete_parameter(Name=name)
        except ssm.exceptions.ParameterNotFound:
            pass


def _call(route_key="GET /api/ping", origin=None, body="{}"):
    headers = {"origin": origin} if origin else {}
    event = {"routeKey": route_key, "headers": headers, "body": None,
             "requestContext": {"http": {"method": route_key.split()[0], "path": route_key.split()[1]}}}
    routes = {"GET /api/ping": lambda e, p, q, b: http_response.success({"ok": True})}
    return http_response.route_request(event, routes)


class TestWhichOriginsAreAllowed:

    def test_the_published_frontend_origin_is_allowed_and_echoed_exactly(self, cors):
        response = _call(origin=f"https://{FRONTEND}")
        assert response["headers"]["Access-Control-Allow-Origin"] == f"https://{FRONTEND}"
        assert response["headers"]["Vary"] == "Origin"

    def test_another_origin_gets_no_cors_header_at_all(self, cors):
        headers = _call(origin="https://evil.example")["headers"]
        assert "Access-Control-Allow-Origin" not in headers

    def test_a_request_with_no_origin_header_is_served_without_cors_headers(self, cors):
        """curl, Quick's server-side calls: not a browser, nothing to allow."""
        response = _call(origin=None)
        assert response["statusCode"] == 200
        assert "Access-Control-Allow-Origin" not in response["headers"]

    def test_a_local_dev_origin_works_once_its_parameter_is_added(self, cors):
        put, fresh_container, _ = cors
        assert "Access-Control-Allow-Origin" not in _call(origin=LOCAL)["headers"]
        put("origin_frontend_local", LOCAL)
        fresh_container()
        assert _call(origin=LOCAL)["headers"]["Access-Control-Allow-Origin"] == LOCAL

    def test_removing_the_local_origin_closes_it_again(self, cors):
        put, fresh_container, ssm = cors
        put("origin_frontend_local", LOCAL)
        fresh_container()
        assert _call(origin=LOCAL)["headers"]["Access-Control-Allow-Origin"] == LOCAL
        ssm.delete_parameter(Name="/corstest/origin_frontend_local")
        fresh_container()
        assert "Access-Control-Allow-Origin" not in _call(origin=LOCAL)["headers"]

    def test_credentialed_requests_are_not_allowed(self, cors):
        """The UI sends its token in Authorization, which needs no credentialed mode."""
        headers = _call(origin=f"https://{FRONTEND}")["headers"]
        assert "Access-Control-Allow-Credentials" not in headers


class TestANewOriginTakesEffectWithoutARedeploy:
    """Adding origin_frontend_local by hand must work in minutes, not at the next cold start."""

    @pytest.fixture
    def clock(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(http_response, "_now", lambda: now[0])
        return now

    def test_the_list_is_read_once_and_reused_within_its_lifetime(self, cors, clock):
        put, _, ssm = cors
        _call(origin=LOCAL)
        put("origin_frontend_local", LOCAL)
        clock[0] += http_response.ORIGIN_CACHE_SECONDS - 1
        assert "Access-Control-Allow-Origin" not in _call(origin=LOCAL)["headers"]

    def test_an_added_origin_is_allowed_once_the_lifetime_has_passed(self, cors, clock):
        put, _, _ = cors
        _call(origin=LOCAL)
        put("origin_frontend_local", LOCAL)
        clock[0] += http_response.ORIGIN_CACHE_SECONDS + 1
        assert _call(origin=LOCAL)["headers"]["Access-Control-Allow-Origin"] == LOCAL

    def test_a_removed_origin_stops_being_allowed_once_the_lifetime_has_passed(self, cors, clock):
        put, _, ssm = cors
        put("origin_frontend_local", LOCAL)
        assert _call(origin=LOCAL)["headers"]["Access-Control-Allow-Origin"] == LOCAL
        ssm.delete_parameter(Name="/corstest/origin_frontend_local")
        clock[0] += http_response.ORIGIN_CACHE_SECONDS + 1
        assert "Access-Control-Allow-Origin" not in _call(origin=LOCAL)["headers"]

    def test_the_lifetime_is_short(self):
        assert 0 < http_response.ORIGIN_CACHE_SECONDS <= 120

    def test_an_emptied_allowlist_is_an_error_not_a_stale_allow(self, cors, clock):
        _, _, ssm = cors
        _call(origin=f"https://{FRONTEND}")
        ssm.delete_parameter(Name="/corstest/origin_frontend")
        clock[0] += http_response.ORIGIN_CACHE_SECONDS + 1
        with pytest.raises(RuntimeError, match="No origin_"):
            _call(origin=f"https://{FRONTEND}")

    def test_a_refresh_ssm_refuses_is_an_error_not_a_stale_allow(self, cors, clock, monkeypatch):
        import botocore.exceptions

        _call(origin=f"https://{FRONTEND}")

        class _Throttled:
            def get_parameters_by_path(self, **kwargs):
                raise botocore.exceptions.ClientError(
                    {"Error": {"Code": "ThrottlingException", "Message": "Rate exceeded"}}, "GetParametersByPath")
        monkeypatch.setattr(http_response, "_ssm_client", _Throttled())
        clock[0] += http_response.ORIGIN_CACHE_SECONDS + 1
        with pytest.raises(botocore.exceptions.ClientError, match="ThrottlingException"):
            _call(origin=f"https://{FRONTEND}")


class TestThePreflight:

    def test_an_allowed_origin_is_told_the_methods_and_headers(self, cors):
        response = _call("OPTIONS /api/connections", origin=f"https://{FRONTEND}")
        assert response["statusCode"] == 204
        headers = response["headers"]
        assert headers["Access-Control-Allow-Origin"] == f"https://{FRONTEND}"
        assert set(headers["Access-Control-Allow-Methods"].split(",")) >= {"GET", "POST", "PATCH", "DELETE", "OPTIONS"}
        assert set(headers["Access-Control-Allow-Headers"].split(",")) >= {"Authorization", "Content-Type"}
        assert int(headers["Access-Control-Max-Age"]) > 0

    def test_another_origin_gets_no_allow_origin_so_the_browser_refuses(self, cors):
        response = _call("OPTIONS /api/connections", origin="https://evil.example")
        assert "Access-Control-Allow-Origin" not in response["headers"]


class TestErrorsAreReadableByTheBrowser:
    """A browser hides a response without the header, so an error a page cannot read
    looks like a network failure."""

    @pytest.mark.parametrize("route_key,status", [("GET /api/nothing", 404)])
    def test_errors_carry_the_origin_header(self, cors, route_key, status):
        response = _call(route_key, origin=f"https://{FRONTEND}")
        assert response["statusCode"] == status
        assert response["headers"]["Access-Control-Allow-Origin"] == f"https://{FRONTEND}"

    def test_a_forbidden_response_carries_it_too(self, cors):
        def admin_only(event, p, q, b):
            raise http_response.ForbiddenError("no")
        event = {"routeKey": "GET /api/admin", "headers": {"origin": f"https://{FRONTEND}"}, "body": None}
        response = http_response.route_request(event, {"GET /api/admin": admin_only})
        assert response["statusCode"] == 403
        assert response["headers"]["Access-Control-Allow-Origin"] == f"https://{FRONTEND}"


class TestItFailsClosed:

    def test_no_origin_parameters_at_all_is_an_error_not_allow_all(self, cors):
        _, fresh_container, ssm = cors
        ssm.delete_parameter(Name="/corstest/origin_frontend")
        fresh_container()
        with pytest.raises(RuntimeError, match="No origin_"):
            _call(origin="https://anything.example")

    def test_a_function_without_a_stack_name_refuses_to_serve(self, cors, monkeypatch):
        monkeypatch.setattr(http_response, "_stack_name", "")
        monkeypatch.setattr(http_response, "_allowed_origins", None)
        with pytest.raises(RuntimeError, match="STACK_NAME"):
            _call(origin="https://anything.example")
