"""Unit tests for x402.http.middleware.asgi - Generic ASGI middleware."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from x402.http.middleware.asgi import (
    ASGIAdapter,
    X402ASGIMiddleware,
    _parse_query_string,
    payment_middleware,
)
from x402.http.types import (
    HTTPProcessResult,
    HTTPResponseInstructions,
    PaymentOption,
    ProcessSettleResult,
    RouteConfig,
)
from x402.schemas import PaymentPayload, PaymentRequirements
from x402.http.facilitator_client_base import FacilitatorResponseError
from x402.schemas.hooks import PaymentCancellationDispatcher


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


def make_asgi_scope(
    method: str = "GET",
    path: str = "/api/test",
    headers: list[tuple[bytes, bytes]] | None = None,
    query_string: bytes = b"",
    state: dict | None = None,
) -> dict:
    """Create a minimal ASGI scope dict."""
    if headers is None:
        headers = [
            (b"host", b"example.com"),
            (b"user-agent", b"TestClient/1.0"),
            (b"accept", b"application/json"),
        ]
    scope: dict = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": headers,
        "query_string": query_string,
        "server": ("example.com", 80),
        "scheme": "http",
    }
    if state:
        scope["state"] = state
    return scope


async def default_send(message: dict) -> None:
    """Default no-op send callable for tests."""
    pass


# =============================================================================
# ASGIAdapter Tests
# =============================================================================


class TestASGIAdapter:
    """Tests for ASGIAdapter."""

    def test_get_header(self):
        """Test getting header value from ASGI scope."""
        scope = make_asgi_scope(
            headers=[(b"x-custom", b"test-value"), (b"host", b"example.com")]
        )
        adapter = ASGIAdapter(scope)

        assert adapter.get_header("x-custom") == "test-value"

    def test_get_header_case_insensitive(self):
        """Test headers are matched case-insensitively."""
        scope = make_asgi_scope(
            headers=[(b"Payment-Signature", b"sig123")]
        )
        adapter = ASGIAdapter(scope)

        assert adapter.get_header("payment-signature") == "sig123"
        assert adapter.get_header("Payment-Signature") == "sig123"
        assert adapter.get_header("PAYMENT-SIGNATURE") == "sig123"

    def test_get_header_missing(self):
        """Test getting missing header returns None."""
        scope = make_asgi_scope()
        adapter = ASGIAdapter(scope)

        assert adapter.get_header("x-missing") is None

    def test_get_method(self):
        """Test getting HTTP method."""
        scope = make_asgi_scope(method="POST")
        adapter = ASGIAdapter(scope)

        assert adapter.get_method() == "POST"

    def test_get_path(self):
        """Test getting request path."""
        scope = make_asgi_scope(path="/api/weather/london")
        adapter = ASGIAdapter(scope)

        assert adapter.get_path() == "/api/weather/london"

    def test_get_url(self):
        """Test getting full URL reconstructed from scope."""
        scope = make_asgi_scope(path="/api/test", query_string=b"key=value")
        adapter = ASGIAdapter(scope)

        assert adapter.get_url() == "http://example.com/api/test?key=value"

    def test_get_url_https(self):
        """Test URL with HTTPS scheme."""
        scope = make_asgi_scope(path="/secure")
        scope["scheme"] = "https"
        scope["server"] = ("example.com", 443)
        adapter = ASGIAdapter(scope)

        assert adapter.get_url() == "https://example.com/secure"

    def test_get_url_custom_port(self):
        """Test URL with non-default port."""
        scope = make_asgi_scope(path="/api")
        scope["server"] = ("example.com", 8080)
        adapter = ASGIAdapter(scope)

        assert adapter.get_url() == "http://example.com:8080/api"

    def test_get_accept_header(self):
        """Test getting Accept header."""
        scope = make_asgi_scope(
            headers=[(b"accept", b"text/html")]
        )
        adapter = ASGIAdapter(scope)

        assert adapter.get_accept_header() == "text/html"

    def test_get_accept_header_missing(self):
        """Test missing Accept returns empty string."""
        scope = make_asgi_scope(headers=[])
        adapter = ASGIAdapter(scope)

        assert adapter.get_accept_header() == ""

    def test_get_user_agent(self):
        """Test getting User-Agent header."""
        scope = make_asgi_scope(
            headers=[(b"user-agent", b"Mozilla/5.0")]
        )
        adapter = ASGIAdapter(scope)

        assert adapter.get_user_agent() == "Mozilla/5.0"

    def test_get_user_agent_missing(self):
        """Test missing User-Agent returns empty string."""
        scope = make_asgi_scope(headers=[])
        adapter = ASGIAdapter(scope)

        assert adapter.get_user_agent() == ""

    def test_get_query_params(self):
        """Test getting query parameters."""
        scope = make_asgi_scope(query_string=b"city=london&units=metric")
        adapter = ASGIAdapter(scope)

        params = adapter.get_query_params()
        assert params["city"] == "london"
        assert params["units"] == "metric"

    def test_get_query_param(self):
        """Test getting single query parameter."""
        scope = make_asgi_scope(query_string=b"city=london")
        adapter = ASGIAdapter(scope)

        assert adapter.get_query_param("city") == "london"

    def test_get_query_param_missing(self):
        """Test missing query parameter returns None."""
        scope = make_asgi_scope()
        adapter = ASGIAdapter(scope)

        assert adapter.get_query_param("missing") is None

    def test_get_body_returns_none(self):
        """Test that get_body returns None."""
        scope = make_asgi_scope()
        adapter = ASGIAdapter(scope)

        assert adapter.get_body() is None


# =============================================================================
# Query String Parsing Tests
# =============================================================================


class TestParseQueryString:
    """Tests for _parse_query_string helper."""

    def test_empty_string(self):
        assert _parse_query_string(b"") == {}

    def test_single_param(self):
        result = _parse_query_string(b"key=value")
        assert result == {"key": "value"}

    def test_multiple_params(self):
        result = _parse_query_string(b"a=1&b=2&c=3")
        assert result == {"a": "1", "b": "2", "c": "3"}

    def test_string_input(self):
        result = _parse_query_string("key=value")
        assert result == {"key": "value"}

    def test_repeated_param_returns_list(self):
        result = _parse_query_string(b"a=1&a=2")
        assert result == {"a": ["1", "2"]}


# =============================================================================
# X402ASGIMiddleware Tests
# =============================================================================


class TestX402ASGIMiddleware:
    """Tests for X402ASGIMiddleware."""

    def test_creates_middleware(self):
        """Test that middleware can be instantiated."""
        mock_app = AsyncMock()
        mock_server = MagicMock()
        routes = {
            "GET /api/test": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to="0x1234567890123456789012345678901234567890",
                    price="$0.01",
                    network="eip155:8453",
                ),
            )
        }

        middleware = X402ASGIMiddleware(mock_app, routes, mock_server, sync_facilitator_on_start=False)

        assert middleware._app is mock_app
        assert callable(middleware)

    def test_non_http_scope_passes_through(self):
        """Test that non-HTTP scope types are passed through."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        middleware = X402ASGIMiddleware(mock_app, {}, mock_server, sync_facilitator_on_start=False)

        lifespan_scope = {"type": "lifespan"}
        import asyncio

        asyncio.get_event_loop().run_until_complete(
            middleware(lifespan_scope, AsyncMock(), AsyncMock())
        )
        mock_app.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_scope_passes_through(self):
        """Test that lifespan scope passes through."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        middleware = X402ASGIMiddleware(mock_app, {}, mock_server, sync_facilitator_on_start=False)

        scope = {"type": "lifespan"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)
        mock_app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_websocket_scope_passes_through(self):
        """Test that WebSocket scope passes through."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        middleware = X402ASGIMiddleware(mock_app, {}, mock_server, sync_facilitator_on_start=False)

        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()

        await middleware(scope, receive, send)
        mock_app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_non_protected_route_passes_through(self):
        """Test that non-protected routes pass through."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = False
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/protected": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/public")
            receive = AsyncMock()
            send = AsyncMock()

            await middleware(scope, receive, send)
            mock_app.assert_awaited_once_with(scope, receive, send)

    @pytest.mark.asyncio
    async def test_facilitator_error_returns_502(self):
        """Test that FacilitatorResponseError returns 502."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.side_effect = FacilitatorResponseError(
                "verify failed"
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            await middleware(scope, receive, capture_send)

            assert len(messages) == 2
            assert messages[0]["type"] == "http.response.start"
            assert messages[0]["status"] == 502
            assert messages[1]["type"] == "http.response.body"
            import json
            body = json.loads(messages[1]["body"])
            assert "verify failed" in body["error"]

    @pytest.mark.asyncio
    async def test_unexpected_error_returns_500(self):
        """Test that unexpected errors return 500."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.side_effect = RuntimeError("boom")
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            await middleware(scope, receive, capture_send)

            assert len(messages) == 2
            assert messages[0]["status"] == 500
            import json
            body = json.loads(messages[1]["body"])
            assert body["error"] == "Internal Server Error"

    @pytest.mark.asyncio
    async def test_payment_error_returns_402(self):
        """Test that payment-error result returns 402 with paywall content."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-error",
                response=HTTPResponseInstructions(
                    status=402,
                    headers={"Content-Type": "application/json"},
                    body={"error": "Payment required"},
                ),
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            await middleware(scope, receive, capture_send)

            assert messages[0]["status"] == 402
            import json
            body = json.loads(messages[1]["body"])
            assert body["error"] == "Payment required"

    @pytest.mark.asyncio
    async def test_payment_verified_stores_in_scope_state(self):
        """Test that payment-verified stores payment info in scope['state']."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
            )
            mock_http_instance.process_settlement.return_value = ProcessSettleResult(
                success=True,
                headers={"PAYMENT-RESPONSE": "settlement_encoded"},
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            # Downstream app should send a response
            async def mock_downstream(s, r, snd):
                # Verify payment info is in scope
                assert "x402_payment_payload" in s.get("state", {})
                assert "x402_payment_requirements" in s.get("state", {})
                await snd({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-type", b"application/json")],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b'{"data": "ok"}',
                })

            middleware._app = mock_downstream

            await middleware(scope, receive, capture_send)

            # Should have response.start and response.body from settlement
            assert messages[0]["type"] == "http.response.start"
            assert messages[0]["status"] == 200

    @pytest.mark.asyncio
    async def test_payment_verified_settlement_failure_returns_402(self):
        """Test that settlement failure returns 402."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
            )
            mock_http_instance.process_settlement.return_value = ProcessSettleResult(
                success=False,
                error_reason="insufficient_funds",
                response=HTTPResponseInstructions(
                    status=402,
                    headers={"PAYMENT-RESPONSE": "failure_data"},
                    body={},
                ),
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream(s, r, snd):
                await snd({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b'{"data": "ok"}',
                })

            middleware._app = mock_downstream

            await middleware(scope, receive, capture_send)

            # Settlement failure should return 402
            assert messages[0]["status"] == 402

    @pytest.mark.asyncio
    async def test_handler_error_status_cancels_settlement(self):
        """Test that handler 4xx/5xx triggers cancellation."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()
        dispatcher = MagicMock(spec=PaymentCancellationDispatcher)
        dispatcher.cancel = AsyncMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
                cancellation_dispatcher=dispatcher,
            )
            mock_http_instance.create_failure_path_settlement_headers.return_value = {
                "PAYMENT-RESPONSE": "cancel_receipt"
            }
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream_error(s, r, snd):
                await snd({
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b'{"error": "failed"}',
                })

            middleware._app = mock_downstream_error

            await middleware(scope, receive, capture_send)

            # Should return error status with cancellation receipt
            assert messages[0]["status"] == 500
            dispatcher.cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_handler_exception_cancels_settlement(self):
        """Test that handler exceptions trigger cancellation."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()
        dispatcher = MagicMock(spec=PaymentCancellationDispatcher)
        dispatcher.cancel = AsyncMock()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
                cancellation_dispatcher=dispatcher,
            )
            mock_http_instance.create_failure_path_settlement_headers.return_value = {
                "PAYMENT-RESPONSE": "cancel_receipt"
            }
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream_exception(s, r, snd):
                raise RuntimeError("handler crashed")

            middleware._app = mock_downstream_exception

            await middleware(scope, receive, capture_send)

            assert messages[0]["status"] == 500
            dispatcher.cancel.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_facilitator_error_during_settlement_returns_502(self):
        """Test FacilitatorResponseError during settlement returns 502."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
            )
            mock_http_instance.process_settlement.side_effect = FacilitatorResponseError(
                "settle verify failed"
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream(s, r, snd):
                await snd({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b"ok",
                })

            middleware._app = mock_downstream

            await middleware(scope, receive, capture_send)

            assert messages[0]["status"] == 502

    @pytest.mark.asyncio
    async def test_unexpected_settlement_error_logs_and_returns_402(self, caplog):
        """Test unexpected settlement error is logged and returns 402."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
            )
            mock_http_instance.process_settlement.side_effect = RuntimeError("RPC timeout")
            mock_http_instance._create_settlement_headers.return_value = {
                "PAYMENT-RESPONSE": "error_receipt"
            }
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(path="/api/test")
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream(s, r, snd):
                await snd({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b"ok",
                })

            middleware._app = mock_downstream

            with caplog.at_level(logging.ERROR):
                await middleware(scope, receive, capture_send)

            assert messages[0]["status"] == 402
            assert any("unexpected error while settling" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_payment_verified_stores_in_existing_state(self):
        """Test that payment info is stored when scope['state'] already exists."""
        mock_app = AsyncMock()
        mock_server = MagicMock()

        payment_payload = make_v2_payload()
        payment_requirements = make_payment_requirements()

        with patch("x402.http.middleware.asgi.x402HTTPResourceServerSync") as mock_http_class:
            mock_http_instance = MagicMock()
            mock_http_instance.requires_payment.return_value = True
            mock_http_instance.process_http_request.return_value = HTTPProcessResult(
                type="payment-verified",
                payment_payload=payment_payload,
                payment_requirements=payment_requirements,
            )
            mock_http_instance.process_settlement.return_value = ProcessSettleResult(
                success=True,
                headers={},
            )
            mock_http_class.return_value = mock_http_instance

            routes = {
                "GET /api/test": RouteConfig(
                    accepts=PaymentOption(
                        scheme="exact",
                        pay_to="0x1234567890123456789012345678901234567890",
                        price="$0.01",
                        network="eip155:8453",
                    ),
                )
            }

            middleware = X402ASGIMiddleware(
                mock_app, routes, mock_server, sync_facilitator_on_start=False
            )

            scope = make_asgi_scope(
                path="/api/test",
                state={"existing_key": "existing_value"},
            )
            receive = AsyncMock()
            messages = []

            async def capture_send(message):
                messages.append(message)

            async def mock_downstream(s, r, snd):
                state = s.get("state", {})
                assert state["existing_key"] == "existing_value"
                assert "x402_payment_payload" in state
                await snd({
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [],
                })
                await snd({
                    "type": "http.response.body",
                    "body": b"ok",
                })

            middleware._app = mock_downstream

            await middleware(scope, receive, capture_send)
            assert messages[0]["status"] == 200


# =============================================================================
# Convenience Function Tests
# =============================================================================


class TestPaymentMiddlewareFactory:
    """Tests for payment_middleware factory function."""

    def test_returns_callable(self):
        """Test that factory returns a callable."""
        mock_server = MagicMock()
        routes = {}

        factory = payment_middleware(routes, mock_server, sync_facilitator_on_start=False)

        assert callable(factory)

    def test_factory_wraps_app(self):
        """Test that factory creates middleware wrapping the app."""
        mock_app = AsyncMock()
        mock_server = MagicMock()
        routes = {}

        factory = payment_middleware(routes, mock_server, sync_facilitator_on_start=False)
        middleware = factory(mock_app)

        assert isinstance(middleware, X402ASGIMiddleware)
        assert middleware._app is mock_app
