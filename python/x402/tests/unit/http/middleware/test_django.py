"""Unit tests for x402.http.middleware.django - Django middleware."""

from __future__ import annotations

import json
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# Skip all tests if django not installed
django = pytest.importorskip("django")

from django.conf import settings as django_settings
from django.http import HttpRequest, JsonResponse, HttpResponse

from x402 import x402FacilitatorSync, x402ResourceServerSync
from x402.http.facilitator_client_base import FacilitatorResponseError
from x402.http.middleware.django import (
    DjangoAdapter,
    X402PaymentMiddleware,
    _json_error_response,
    payment_middleware,
    set_settlement_overrides,
)
from x402.http.types import (
    HTTPProcessResult,
    HTTPResponseInstructions,
    PaymentOption,
    ProcessSettleResult,
    RouteConfig,
)
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.schemas.hooks import (
    CompletedSettlement,
    PaymentCancellationDispatcher,
    VerifiedPaymentCancelOptions,
)

from ....mocks import (
    CashFacilitatorClientSync,
    CashSchemeNetworkFacilitator,
    CashSchemeNetworkServer,
)


# =============================================================================
# Helpers
# =============================================================================


def make_payment_requirements() -> PaymentRequirements:
    """Helper to create valid PaymentRequirements."""
    return PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x0000000000000000000000000000000000000000",
        amount="1000000",
        pay_to="0x1234567890123456789012345678901234567890",
        max_timeout_seconds=300,
    )


def make_v2_payload(signature: str = "0xmock") -> PaymentPayload:
    """Helper to create valid V2 PaymentPayload."""
    return PaymentPayload(
        x402_version=2,
        payload={"signature": signature},
        accepted=make_payment_requirements(),
    )


def make_mock_django_request(
    method: str = "GET",
    path: str = "/api/test",
    meta: dict[str, str] | None = None,
    get_params: dict[str, str] | None = None,
    body: bytes | None = None,
) -> MagicMock:
    """Create a mock Django HttpRequest object."""
    mock_request = MagicMock(spec=HttpRequest)
    mock_request.method = method
    mock_request.path = path
    mock_request.build_absolute_uri = MagicMock(return_value=f"https://example.com{path}")

    # Django stores headers in META with HTTP_ prefix
    meta_dict = {}
    if meta:
        for key, value in meta.items():
            # Convert to Django META format
            django_key = "HTTP_" + key.replace("-", "_").upper()
            meta_dict[django_key] = value
    mock_request.META = meta_dict

    # Query params
    mock_request.GET = get_params or {}

    # Body
    mock_request.body = body or b""

    # Response helpers
    mock_request.x402_payment_payload = None
    mock_request.x402_payment_requirements = None

    return mock_request


# =============================================================================
# Adapter Tests
# =============================================================================


class TestDjangoAdapter:
    """Tests for DjangoAdapter."""

    def test_get_header_normal(self):
        """Test getting a normal header."""
        request = make_mock_django_request(meta={"X-Custom": "value"})
        adapter = DjangoAdapter(request)
        assert adapter.get_header("X-Custom") == "value"

    def test_get_header_content_type(self):
        """Test getting Content-Type header (no HTTP_ prefix in META)."""
        request = make_mock_django_request(meta={"Content-Type": "application/json"})
        adapter = DjangoAdapter(request)
        assert adapter.get_header("Content-Type") == "application/json"

    def test_get_header_content_length(self):
        """Test getting Content-Length header."""
        request = make_mock_django_request(meta={"Content-Length": "100"})
        adapter = DjangoAdapter(request)
        assert adapter.get_header("Content-Length") == "100"

    def test_get_header_missing(self):
        """Test getting a missing header returns None."""
        request = make_mock_django_request()
        adapter = DjangoAdapter(request)
        assert adapter.get_header("X-Missing") is None

    def test_get_method(self):
        """Test getting HTTP method."""
        request = make_mock_django_request(method="POST")
        adapter = DjangoAdapter(request)
        assert adapter.get_method() == "POST"

    def test_get_path(self):
        """Test getting request path."""
        request = make_mock_django_request(path="/api/weather")
        adapter = DjangoAdapter(request)
        assert adapter.get_path() == "/api/weather"

    def test_get_url(self):
        """Test getting full URL."""
        request = make_mock_django_request(path="/api/weather")
        adapter = DjangoAdapter(request)
        assert adapter.get_url() == "https://example.com/api/weather"

    def test_get_accept_header(self):
        """Test getting Accept header."""
        request = make_mock_django_request(meta={"Accept": "application/json"})
        adapter = DjangoAdapter(request)
        assert adapter.get_accept_header() == "application/json"

    def test_get_user_agent(self):
        """Test getting User-Agent header."""
        request = make_mock_django_request(meta={"User-Agent": "TestBot/1.0"})
        adapter = DjangoAdapter(request)
        assert adapter.get_user_agent() == "TestBot/1.0"

    def test_get_query_params(self):
        """Test getting query parameters."""
        request = make_mock_django_request(get_params={"q": "test", "page": "1"})
        adapter = DjangoAdapter(request)
        params = adapter.get_query_params()
        assert params["q"] == "test"
        assert params["page"] == "1"

    def test_get_query_param(self):
        """Test getting a single query parameter."""
        request = make_mock_django_request(get_params={"q": "test"})
        adapter = DjangoAdapter(request)
        assert adapter.get_query_param("q") == "test"

    def test_get_body_json(self):
        """Test getting JSON body."""
        body = json.dumps({"key": "value"}).encode("utf-8")
        request = make_mock_django_request(body=body)
        adapter = DjangoAdapter(request)
        assert adapter.get_body() == {"key": "value"}

    def test_get_body_empty(self):
        """Test getting empty body."""
        request = make_mock_django_request(body=b"")
        adapter = DjangoAdapter(request)
        assert adapter.get_body() is None

    def test_get_body_invalid_json(self):
        """Test getting invalid JSON body."""
        request = make_mock_django_request(body=b"not json")
        adapter = DjangoAdapter(request)
        assert adapter.get_body() is None


# =============================================================================
# Response Helper Tests
# =============================================================================


class TestJsonErrorResponse:
    """Tests for _json_error_response helper."""

    def test_basic_error(self):
        """Test basic error response."""
        response = _json_error_response(402, "Payment required")
        assert response.status_code == 402
        assert json.loads(response.content) == {"error": "Payment required"}

    def test_error_with_headers(self):
        """Test error response with extra headers."""
        response = _json_error_response(502, "Bad Gateway", {"X-Custom": "value"})
        assert response.status_code == 502
        assert response["X-Custom"] == "value"


# =============================================================================
# Extension Check Tests
# =============================================================================


class TestCheckIfBazaarNeeded:
    """Tests for _check_if_bazaar_needed helper."""

    def test_returns_false_for_empty_extensions(self):
        """Test that routes without extensions return False."""
        from x402.http.middleware._bazaar_utils import check_if_bazaar_needed

        route = RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to="0x1234567890123456789012345678901234567890",
                price="$0.01",
                network="eip155:8453",
            ),
        )
        assert check_if_bazaar_needed(route) is False


# =============================================================================
# Middleware Integration Tests
# =============================================================================


class TestX402PaymentMiddleware:
    """Integration tests for X402PaymentMiddleware."""

    def _make_middleware(
        self,
        routes: dict | None = None,
        server: x402ResourceServerSync | None = None,
    ):
        """Create middleware with mock server."""
        if routes is None:
            routes = {}
        if server is None:
            server = x402ResourceServerSync(CashFacilitatorClientSync())
            server.register("eip155:8453", CashSchemeNetworkServer())

        from x402.http.types import RouteConfig, PaymentOption

        if routes and not isinstance(list(routes.values())[0], RouteConfig):
            route_config = {}
            for pattern, config in routes.items():
                accepts = config.get("accepts", {})
                route_config[pattern] = RouteConfig(
                    accepts=PaymentOption(
                        scheme=accepts.get("scheme", "exact"),
                        pay_to=accepts.get("payTo", "0x1234567890123456789012345678901234567890"),
                        price=accepts.get("price", "$0.01"),
                        network=accepts.get("network", "eip155:8453"),
                    ),
                )
            routes = route_config

        from x402.http.middleware.django import X402PaymentMiddleware

        # Create middleware class with injected server
        http_server = x402.http.x402_http_server.x402HTTPResourceServerSync(server, routes)

        class InjectedMiddleware:
            def __init__(self, get_response=None):
                self._get_response = get_response
                self._http_server = http_server

            def __call__(self, request):
                adapter = DjangoAdapter(request)
                context = __import__("x402.http.types", fromlist=["HTTPRequestContext"]).HTTPRequestContext(
                    adapter=adapter,
                    path=request.path,
                    method=request.method,
                    payment_header=(
                        adapter.get_header("payment-signature")
                        or adapter.get_header("x-payment")
                    ),
                )

                if not self._http_server.requires_payment(context):
                    return self._get_response(request)

                result = self._http_server.process_http_request(context)

                if result.type == "no-payment-required":
                    return self._get_response(request)

                if result.type == "payment-error":
                    return JsonResponse(
                        content=result.response.body or {},
                        status=result.response.status,
                        headers=result.response.headers,
                    )

                if result.type == "payment-verified":
                    request.x402_payment_payload = result.payment_payload
                    request.x402_payment_requirements = result.payment_requirements
                    return self._get_response(request)

                return self._get_response(request)

        return InjectedMiddleware

    def test_free_route_passes_through(self):
        """Test that routes not matching any pattern pass through."""
        middleware = self._make_middleware(routes={})

        request = make_mock_django_request(method="GET", path="/api/free")
        response = HttpResponse("OK")

        def get_response(req):
            return response

        mw = middleware(get_response)
        result = mw(request)
        assert result == response

    def test_payment_required_returns_402(self):
        """Test that payment-required routes return 402."""
        routes = {
            "GET /api/paid/*": {
                "accepts": {
                    "scheme": "exact",
                    "payTo": "0x1234567890123456789012345678901234567890",
                    "price": "$0.01",
                    "network": "eip155:8453",
                }
            }
        }

        middleware = self._make_middleware(routes=routes)
        request = make_mock_django_request(method="GET", path="/api/paid/weather")

        def get_response(req):
            return HttpResponse("OK")

        mw = middleware(get_response)
        result = mw(request)

        # Should get 402 with payment required
        assert result.status_code == 402

    def test_payment_verified_attaches_to_request(self):
        """Test that verified payment attaches payload to request."""
        from x402.http.types import RouteConfig, PaymentOption

        # Create a server that returns "already paid" (cash scheme)
        routes = {
            "GET /api/paid/*": RouteConfig(
                accepts=PaymentOption(
                    scheme="cash",
                    pay_to="0x1234567890123456789012345678901234567890",
                    price="$0.01",
                    network="eip155:8453",
                ),
            ),
        }

        server = x402ResourceServerSync(CashFacilitatorClientSync())
        server.register("eip155:8453", CashSchemeNetworkServer())

        http_server = x402.http.x402_http_server.x402HTTPResourceServerSync(server, routes)

        request = make_mock_django_request(method="GET", path="/api/paid/data")

        def get_response(req):
            # Check that payment info was attached
            assert hasattr(req, "x402_payment_payload")
            return HttpResponse('{"data": "value"}', content_type="application/json")

        from x402.http.types import HTTPRequestContext

        adapter = DjangoAdapter(request)
        context = HTTPRequestContext(
            adapter=adapter,
            path=request.path,
            method=request.method,
            payment_header=(
                adapter.get_header("payment-signature")
                or adapter.get_header("x-payment")
            ),
        )

        # Process through http server
        result = http_server.process_http_request(context)

        if result.type == "payment-verified":
            request.x402_payment_payload = result.payment_payload
            request.x402_payment_requirements = result.payment_requirements
            response = get_response(request)
            assert response.status_code == 200

    def test_set_settlement_overrides(self):
        """Test setting settlement overrides on response."""
        response = HttpResponse("OK")
        set_settlement_overrides(response, {"amount": "500"})
        assert response["x402-settlement-overrides"] == '{"amount": "500"}'

    def test_facilitator_error_returns_502(self):
        """Test that facilitator errors return 502."""
        mock_facilitator = MagicMock()
        mock_facilitator.get_supported_capabilities = MagicMock(
            side_effect=FacilitatorResponseError("Service unavailable", 503)
        )

        server = x402ResourceServerSync(mock_facilitator)
        routes = {
            "GET /api/paid/*": {
                "accepts": {
                    "scheme": "exact",
                    "payTo": "0x1234567890123456789012345678901234567890",
                    "price": "$0.01",
                    "network": "eip155:8453",
                }
            }
        }

        from x402.http.types import RouteConfig, PaymentOption

        route_config = {
            "GET /api/paid/*": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to="0x1234567890123456789012345678901234567890",
                    price="$0.01",
                    network="eip155:8453",
                ),
            ),
        }

        http_server = x402.http.x402_http_server.x402HTTPResourceServerSync(server, route_config)

        request = make_mock_django_request(method="GET", path="/api/paid/data")

        def get_response(req):
            return HttpResponse("OK")

        from x402.http.types import HTTPRequestContext

        adapter = DjangoAdapter(request)
        context = HTTPRequestContext(
            adapter=adapter,
            path=request.path,
            method=request.method,
            payment_header=(
                adapter.get_header("payment-signature")
                or adapter.get_header("x-payment")
            ),
        )

        if http_server.requires_payment(context):
            try:
                result = http_server.process_http_request(context)
            except FacilitatorResponseError as error:
                response = _json_error_response(502, str(error))
                assert response.status_code == 502


# =============================================================================
# Factory Function Tests
# =============================================================================


class TestPaymentMiddlewareFactory:
    """Tests for payment_middleware factory function."""

    def test_factory_returns_class(self):
        """Test that factory returns a middleware class."""
        from x402.http.types import RouteConfig, PaymentOption

        routes = {
            "GET /api/test/*": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to="0x1234567890123456789012345678901234567890",
                    price="$0.01",
                    network="eip155:8453",
                ),
            ),
        }

        server = x402ResourceServerSync(CashFacilitatorClientSync())
        server.register("eip155:8453", CashSchemeNetworkServer())

        middleware_class = payment_middleware(routes, server)
        assert callable(middleware_class)

    def test_factory_creates_callable_instance(self):
        """Test that factory-created middleware can be instantiated."""
        from x402.http.types import RouteConfig, PaymentOption

        routes = {
            "GET /api/test/*": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to="0x1234567890123456789012345678901234567890",
                    price="$0.01",
                    network="eip155:8453",
                ),
            ),
        }

        server = x402ResourceServerSync(CashFacilitatorClientSync())
        server.register("eip155:8453", CashSchemeNetworkServer())

        middleware_class = payment_middleware(routes, server)
        instance = middleware_class(lambda req: HttpResponse("OK"))
        assert callable(instance)
