"""Generic ASGI middleware for x402 payment handling.

Provides payment-gated route protection for ANY ASGI framework
(Litestar, BlackSheep, Quart, Starlette, FastAPI, etc.).

This middleware operates at the raw ASGI level, requiring no framework-specific
dependencies. Payment info is stored in ``scope['state']`` for downstream
access.

Example:
    ```python
    from x402.http.middleware.asgi import X402ASGIMiddleware

    # Wrap any ASGI app
    app = X402ASGIMiddleware(
        app=some_asgi_app,
        routes=routes,
        server=resource_server,
    )
    ```
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from ...schemas import SettleResponse, VerifiedPaymentCancelOptions
from ..background_init import handle_background_init_error
from ..constants import SETTLEMENT_OVERRIDES_HEADER
from ..facilitator_client_base import FacilitatorResponseError
from ..types import (
    HTTPAdapter,
    HTTPRequestContext,
    HTTPTransportContext,
    PaywallConfig,
    RoutesConfig,
)
from ..x402_http_server import PaywallProvider, x402HTTPResourceServerSync
from ..x402_http_server_base import PAYMENT_REQUIRED_CACHE_CONTROL, with_private_cache_control

if TYPE_CHECKING:
    from ...server import x402ResourceServerSync


# ============================================================================
# Extension Auto-Registration
# ============================================================================

from ._bazaar_utils import (
    check_if_bazaar_needed as _check_if_bazaar_needed,
)
from ._bazaar_utils import (
    register_bazaar_extension as _register_bazaar_extension,
)
from ._bazaar_utils import (
    validate_bazaar_extensions as _validate_bazaar_extensions,
)

logger = logging.getLogger(__name__)

# ============================================================================
# ASGI Adapter
# ============================================================================

ASGIReceive = Callable[..., Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]


class ASGIAdapter(HTTPAdapter):
    """Adapter for raw ASGI scope.

    Implements HTTPAdapter protocol by extracting headers, method, path,
    and other request info from the ASGI scope dict.

    ASGI headers are a list of 2-tuples ``[(name, value), ...]`` where
    both name and value are byte strings and names are lowercase.
    """

    def __init__(
        self,
        scope: dict[str, Any],
        query_params: dict[str, str | list[str]] | None = None,
    ) -> None:
        """Create adapter from ASGI scope.

        Args:
            scope: ASGI connection scope dict.
            query_params: Parsed query parameters (optional).
        """
        self._scope = scope
        self._query_params = query_params or {}
        # Build a case-insensitive header lookup dict from byte tuples
        self._headers: dict[str, str] = {}
        for name_bytes, value_bytes in scope.get("headers", []):
            name = name_bytes.decode("latin-1").lower()
            value = value_bytes.decode("latin-1")
            self._headers[name] = value

    def get_header(self, name: str) -> str | None:
        """Get header value (case-insensitive).

        Args:
            name: Header name.

        Returns:
            Header value or None.
        """
        return self._headers.get(name.lower())

    def get_method(self) -> str:
        """Get HTTP method.

        Returns:
            HTTP method (GET, POST, etc.).
        """
        return self._scope.get("method", "GET")

    def get_path(self) -> str:
        """Get request path.

        Returns:
            Request path.
        """
        return self._scope.get("path", "/")

    def get_url(self) -> str:
        """Get full request URL.

        Reconstructed from ASGI scope fields.

        Returns:
            Full URL string.
        """
        scheme = self._scope.get("scheme", "http")
        server = self._scope.get("server")
        host = server[0] if server else "localhost"
        port = server[1] if server and len(server) > 1 else 80

        # Omit default port for scheme
        if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
            authority = host
        else:
            authority = f"{host}:{port}"

        path = self._scope.get("path", "/")
        query_string = self._scope.get("query_string", b"")
        if query_string:
            qs = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
            return f"{scheme}://{authority}{path}?{qs}"
        return f"{scheme}://{authority}{path}"

    def get_accept_header(self) -> str:
        """Get Accept header.

        Returns:
            Accept header value.
        """
        return self._headers.get("accept", "")

    def get_user_agent(self) -> str:
        """Get User-Agent header.

        Returns:
            User-Agent header value.
        """
        return self._headers.get("user-agent", "")

    def get_query_params(self) -> dict[str, str | list[str]]:
        """Get query parameters.

        Returns:
            Dict of query parameters.
        """
        return dict(self._query_params)

    def get_query_param(self, name: str) -> str | list[str] | None:
        """Get single query parameter.

        Args:
            name: Parameter name.

        Returns:
            Parameter value or None.
        """
        return self._query_params.get(name)

    def get_body(self) -> Any:
        """Get request body.

        Returns:
            None (body requires async access via receive).
        """
        return None


# ============================================================================
# ASGI Response Helpers
# ============================================================================


async def _send_asgi_response(
    send: ASGISend,
    status_code: int,
    headers: list[tuple[str, str]],
    body: bytes,
) -> None:
    """Send a complete ASGI HTTP response asynchronously.

    Args:
        send: ASGI send callable.
        status_code: HTTP status code.
        headers: Response headers as list of (name, value) tuples.
        body: Response body bytes.
    """
    asgi_headers = [
        (name.encode("latin-1"), value.encode("latin-1")) for name, value in headers
    ]

    await send({
        "type": "http.response.start",
        "status": status_code,
        "headers": asgi_headers,
    })
    await send({
        "type": "http.response.body",
        "body": body,
    })


def _json_bytes(data: Any) -> bytes:
    """Serialize data to JSON bytes."""
    return json.dumps(data).encode("utf-8")


def _parse_query_string(query_string: bytes | str) -> dict[str, str | list[str]]:
    """Parse ASGI query string into a dict.

    Args:
        query_string: Raw query string bytes or string.

    Returns:
        Parsed query parameters.
    """
    from urllib.parse import parse_qs

    if isinstance(query_string, bytes):
        query_string = query_string.decode("latin-1")

    if not query_string:
        return {}

    parsed = parse_qs(query_string, keep_blank_values=True)
    # Flatten single-value lists
    result: dict[str, str | list[str]] = {}
    for key, values in parsed.items():
        result[key] = values[0] if len(values) == 1 else values
    return result


# ============================================================================
# ASGI Middleware Class
# ============================================================================


class X402ASGIMiddleware:
    """Generic ASGI middleware for x402 payment handling.

    Works with ANY ASGI framework (Litestar, BlackSheep, Quart, Starlette,
    FastAPI, etc.). No framework-specific dependencies required.

    Payment info is stored in ``scope['state']['x402_payment_payload']`` and
    ``scope['state']['x402_payment_requirements']`` for downstream handlers.

    Example:
        ```python
        from x402.http.middleware.asgi import X402ASGIMiddleware

        app = X402ASGIMiddleware(
            app=your_asgi_app,
            routes=routes,
            server=resource_server,
        )
        ```
    """

    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        routes: RoutesConfig,
        server: x402ResourceServerSync,
        paywall_config: PaywallConfig | None = None,
        paywall_provider: PaywallProvider | None = None,
        sync_facilitator_on_start: bool = True,
    ) -> None:
        """Initialize ASGI middleware.

        Args:
            app: ASGI application callable.
            routes: Route configuration for protected endpoints.
            server: Pre-configured x402ResourceServerSync.
            paywall_config: Optional paywall UI configuration.
            paywall_provider: Optional custom paywall provider.
            sync_facilitator_on_start: Fetch facilitator support when created.
        """
        # Auto-register bazaar extension if routes declare it
        if _check_if_bazaar_needed(routes):
            _register_bazaar_extension(server)
            _validate_bazaar_extensions(routes)

        self._app = app
        self._http_server = x402HTTPResourceServerSync(server, routes)
        self._paywall_config = paywall_config
        self._sync_on_start = sync_facilitator_on_start
        self._init_done = False
        self._init_lock = threading.Lock()

        if paywall_provider:
            self._http_server.register_paywall_provider(paywall_provider)

        if self._sync_on_start:
            try:
                self._http_server.initialize()
                self._init_done = True
            except Exception as error:
                handle_background_init_error(error)

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        """ASGI entry point.

        Intercepts HTTP requests on payment-protected routes and processes
        x402 payments before forwarding to the downstream ASGI app.

        Lifespan and WebSocket scope types are passed through directly.

        Args:
            scope: ASGI connection scope.
            receive: ASGI receive callable.
            send: ASGI send callable.
        """
        # Only intercept HTTP requests
        if scope.get("type") == "lifespan":
            await self._app(scope, receive, send)
            return

        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return

        # Parse query string
        query_params = _parse_query_string(scope.get("query_string", b""))

        # Create adapter and context
        adapter = ASGIAdapter(scope, query_params)
        context = HTTPRequestContext(
            adapter=adapter,
            path=adapter.get_path(),
            method=adapter.get_method(),
            payment_header=(
                adapter.get_header("payment-signature")
                or adapter.get_header("x-payment")
            ),
        )

        # Check if route requires payment
        if not self._http_server.requires_payment(context):
            await self._app(scope, receive, send)
            return

        # Initialize on first protected request (double-checked locking)
        if self._sync_on_start and not self._init_done:
            with self._init_lock:
                if not self._init_done:
                    try:
                        self._http_server.initialize()
                    except FacilitatorResponseError as error:
                        await _send_asgi_response(
                            send,
                            502,
                            [("Content-Type", "application/json")],
                            _json_bytes({"error": str(error)}),
                        )
                        return
                    self._init_done = True

        # Process payment request synchronously
        try:
            result = self._http_server.process_http_request(context, self._paywall_config)
        except FacilitatorResponseError as error:
            await _send_asgi_response(
                send,
                502,
                [("Content-Type", "application/json")],
                _json_bytes({"error": str(error)}),
            )
            return
        except Exception:
            logger.exception("x402: unexpected error while processing an HTTP payment request")
            await _send_asgi_response(
                send,
                500,
                [("Content-Type", "application/json")],
                _json_bytes({"error": "Internal Server Error"}),
            )
            return

        if result.type == "no-payment-required":
            await self._app(scope, receive, send)
            return

        if result.type == "payment-error":
            response = result.response
            if response is None:
                await _send_asgi_response(
                    send,
                    402,
                    [("Content-Type", "application/json")],
                    _json_bytes({"error": "Payment required"}),
                )
                return

            headers = list(response.headers.items())
            if response.is_html:
                headers.append(("Content-Type", "text/html; charset=utf-8"))
                body = (
                    response.body.encode("utf-8")
                    if isinstance(response.body, str)
                    else response.body if isinstance(response.body, bytes) else _json_bytes(response.body)
                )
            else:
                headers.append(("Content-Type", "application/json"))
                body = _json_bytes(response.body or {})

            await _send_asgi_response(send, response.status, headers, body)
            return

        if result.type == "payment-verified":
            # Store payment info in ASGI scope['state'] for downstream access
            state = scope.setdefault("state", {})
            state["x402_payment_payload"] = result.payment_payload
            state["x402_payment_requirements"] = result.payment_requirements

            dispatcher = result.cancellation_dispatcher
            transport_context = HTTPTransportContext(request=context)

            # Capture the downstream response
            captured_status = 200
            captured_headers: list[tuple[bytes, bytes]] = []
            captured_body = b""

            async def capture_send(message: dict[str, Any]) -> None:
                nonlocal captured_status, captured_headers, captured_body
                if message["type"] == "http.response.start":
                    captured_status = message.get("status", 200)
                    captured_headers = list(message.get("headers", []))
                elif message["type"] == "http.response.body":
                    captured_body += message.get("body", b"")

            # Call downstream
            try:
                await self._app(scope, receive, capture_send)
            except Exception as error:
                cancel_settlement = None
                if dispatcher is not None:
                    cancel_settlement = await dispatcher.cancel(
                        VerifiedPaymentCancelOptions(reason="handler_threw", error=error)
                    )
                failure_headers = self._http_server.create_failure_path_settlement_headers(
                    cancel_settlement,
                    result.before_handler_settlement,
                    result.payment_payload,
                )
                if not isinstance(failure_headers, dict) or not failure_headers:
                    raise
                await _send_asgi_response(
                    send,
                    500,
                    [("Content-Type", "application/json"), *failure_headers.items()],
                    _json_bytes({"error": "Internal Server Error"}),
                )
                return

            # Don't settle on error responses
            if captured_status >= 400:
                cancel_settlement = None
                if dispatcher is not None:
                    cancel_settlement = await dispatcher.cancel(
                        VerifiedPaymentCancelOptions(
                            reason="handler_failed",
                            response_status=captured_status,
                        )
                    )
                existing_cc = None
                for name_bytes, value_bytes in captured_headers:
                    if name_bytes.lower() == b"cache-control":
                        existing_cc = value_bytes.decode("latin-1")
                        break
                failure_headers = self._http_server.create_failure_path_settlement_headers(
                    cancel_settlement,
                    result.before_handler_settlement,
                    result.payment_payload,
                    existing_cc,
                )
                if isinstance(failure_headers, dict):
                    for key, value in failure_headers.items():
                        captured_headers.append((key.encode("latin-1"), value.encode("latin-1")))
                await _send_asgi_response(
                    send,
                    captured_status,
                    [(n.decode("latin-1"), v.decode("latin-1")) for n, v in captured_headers],
                    captured_body,
                )
                return

            # Convert captured headers to dict for processing
            response_headers_dict: dict[str, str] = {}
            for name_bytes, value_bytes in captured_headers:
                name = name_bytes.decode("latin-1")
                value = value_bytes.decode("latin-1")
                response_headers_dict[name] = value

            # Extract and strip settlement overrides
            overrides = self._http_server._extract_settlement_overrides(response_headers_dict)
            if overrides is not None:
                captured_headers = [
                    (n, v)
                    for n, v in captured_headers
                    if n.lower() != SETTLEMENT_OVERRIDES_HEADER.lower().encode("latin-1")
                ]

            transport_context.response_headers = response_headers_dict

            # Process settlement (sync)
            try:
                settle_result = self._http_server.process_settlement(
                    result.payment_payload,
                    result.payment_requirements,
                    context=context,
                    settlement_overrides=overrides,
                    declared_extensions=result.declared_extensions,
                    transport_context=transport_context,
                    before_handler_settlement=result.before_handler_settlement,
                )

                if not settle_result.success:
                    settle_response = settle_result.response
                    if settle_response is None:
                        await _send_asgi_response(
                            send, 402, [("Content-Type", "application/json")], b"{}"
                        )
                        return
                    if settle_response.is_html:
                        resp_headers = list(settle_response.headers.items())
                        resp_headers.append(("Content-Type", "text/html; charset=utf-8"))
                        body = (
                            settle_response.body.encode("utf-8")
                            if isinstance(settle_response.body, str)
                            else settle_response.body if isinstance(settle_response.body, bytes)
                            else _json_bytes(settle_response.body)
                        )
                    else:
                        resp_headers = list(settle_response.headers.items())
                        resp_headers.append(("Content-Type", "application/json"))
                        body = _json_bytes(settle_response.body or {})
                    await _send_asgi_response(send, settle_response.status, resp_headers, body)
                    return

                # Build final response with settlement headers
                final_headers = list(captured_headers)
                for key, value in settle_result.headers.items():
                    final_headers.append((key.encode("latin-1"), value.encode("latin-1")))

                # Update Cache-Control to include 'private'
                cache_control_updated = False
                for i, (name_bytes, _) in enumerate(final_headers):
                    if name_bytes.lower() == b"cache-control":
                        existing_cc = name_bytes.decode("latin-1")
                        # Get the value
                        val = final_headers[i][1].decode("latin-1")
                        final_headers[i] = (
                            b"cache-control",
                            with_private_cache_control(val).encode("latin-1"),
                        )
                        cache_control_updated = True
                        break
                if not cache_control_updated:
                    final_headers.append((b"cache-control", b"private"))

                await _send_asgi_response(
                    send,
                    captured_status,
                    [(n.decode("latin-1"), v.decode("latin-1")) for n, v in final_headers],
                    captured_body,
                )

            except FacilitatorResponseError as error:
                await _send_asgi_response(
                    send,
                    502,
                    [("Content-Type", "application/json")],
                    _json_bytes({"error": str(error)}),
                )
            except Exception:
                logger.exception("x402: unexpected error while settling a verified payment")
                settle_response = SettleResponse(
                    success=False,
                    error_reason="unexpected_settle_error",
                    error_message="unexpected error while settling the verified payment",
                    transaction="",
                    network=result.payment_requirements.network,
                )
                settle_headers = self._http_server._create_settlement_headers(
                    settle_response, result.payment_requirements
                )
                await _send_asgi_response(
                    send,
                    402,
                    [
                        ("Content-Type", "application/json"),
                        ("Cache-Control", PAYMENT_REQUIRED_CACHE_CONTROL),
                        *settle_headers.items(),
                    ],
                    b"{}",
                )
            return

        # Fallthrough — pass through to downstream app
        await self._app(scope, receive, send)


# ============================================================================
# Convenience Functions
# ============================================================================


def payment_middleware(
    routes: RoutesConfig,
    server: x402ResourceServerSync,
    paywall_config: PaywallConfig | None = None,
    paywall_provider: PaywallProvider | None = None,
    sync_facilitator_on_start: bool = True,
) -> Callable[
    [Callable[..., Awaitable[None]]],
    X402ASGIMiddleware,
]:
    """Create a middleware factory for any ASGI framework.

    Returns a callable that wraps an ASGI app with x402 payment protection.

    Args:
        routes: Route configuration for protected endpoints.
        server: Pre-configured x402ResourceServerSync.
        paywall_config: Optional paywall UI configuration.
        paywall_provider: Optional custom paywall provider.
        sync_facilitator_on_start: Fetch facilitator support when created.

    Returns:
        Callable that wraps an ASGI app.

    Example:
        ```python
        from x402.http.middleware.asgi import payment_middleware

        middleware = payment_middleware(routes, server)
        app = middleware(your_asgi_app)
        ```
    """
    def _wrap(app: Callable[..., Awaitable[None]]) -> X402ASGIMiddleware:
        return X402ASGIMiddleware(
            app=app,
            routes=routes,
            server=server,
            paywall_config=paywall_config,
            paywall_provider=paywall_provider,
            sync_facilitator_on_start=sync_facilitator_on_start,
        )
    return _wrap


def payment_middleware_from_config(
    routes: RoutesConfig,
    facilitator_client: Any = None,
    schemes: list[dict[str, Any]] | None = None,
    paywall_config: PaywallConfig | None = None,
    paywall_provider: PaywallProvider | None = None,
    sync_facilitator_on_start: bool = True,
) -> Callable[
    [Callable[..., Awaitable[None]]],
    X402ASGIMiddleware,
]:
    """Create ASGI payment middleware from configuration.

    Convenience function that creates x402ResourceServerSync internally.

    Args:
        routes: Route configuration for protected endpoints.
        facilitator_client: Facilitator client(s) for payment processing.
        schemes: Scheme registrations for server-side processing.
        paywall_config: Optional paywall UI configuration.
        paywall_provider: Optional custom paywall provider.
        sync_facilitator_on_start: Fetch facilitator support when created.

    Returns:
        Callable that wraps an ASGI app.
    """
    from ...server import x402ResourceServerSync as _x402ResourceServerSync

    server = _x402ResourceServerSync(facilitator_client)

    if schemes:
        for registration in schemes:
            server.register(registration["network"], registration["server"])

    return payment_middleware(
        routes,
        server,
        paywall_config,
        paywall_provider,
        sync_facilitator_on_start,
    )


def set_settlement_overrides(scope: dict[str, Any], overrides: dict[str, Any]) -> None:
    """Settlement overrides are not applicable to raw ASGI middleware.

    In raw ASGI, the downstream app sends the response directly. Use
    ``X402ASGIMiddleware`` which handles settlement transparently.

    Raises:
        NotImplementedError: Always, as raw ASGI has no response object to annotate.
    """
    raise NotImplementedError(
        "set_settlement_overrides is not supported in raw ASGI middleware. "
        "The middleware handles settlement transparently."
    )
