"""Django middleware for x402 payment handling.

Provides payment-gated route protection for Django applications.
Uses x402HTTPResourceServerSync for synchronous request processing.

Example:
    ```python
    # settings.py
    MIDDLEWARE = [
        "x402.http.middleware.django.X402PaymentMiddleware",
        # ... other middleware
    ]

    X402_ROUTES = {
        "GET /api/weather/*": {
            "accepts": {
                "scheme": "exact",
                "payTo": "0x...",
                "price": "$0.01",
                "network": "eip155:84532",
            },
            "description": "Weather API",
        },
    }
    X402_FACILITATOR_URL = "https://x402.org/facilitator"
    ```
"""

from __future__ import annotations

import json
import logging
import threading
from typing import TYPE_CHECKING, Any

try:
    from django.conf import settings
    from django.http import HttpRequest, JsonResponse, HttpResponse
except ImportError as e:
    raise ImportError(
        "Django middleware requires django. Install with: uv add x402[django]"
    ) from e

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
# Django Adapter
# ============================================================================


class DjangoAdapter(HTTPAdapter):
    """Adapter for Django HttpRequest.

    Implements HTTPAdapter protocol for Django framework.
    """

    def __init__(self, request: HttpRequest) -> None:
        """Create adapter from Django request.

        Args:
            request: Django HttpRequest object.
        """
        self._request = request

    def get_header(self, name: str) -> str | None:
        """Get header value (case-insensitive).

        Django stores headers in META with HTTP_ prefix and uppercased.
        Also handles Content-Type and Content-Length which lack the prefix.

        Args:
            name: Header name.

        Returns:
            Header value or None.
        """
        # Django normalizes headers to META: "Content-Type" -> "CONTENT_TYPE",
        # "X-Custom" -> "HTTP_X_CUSTOM"
        meta_key = name.replace("-", "_").upper()
        if meta_key in ("CONTENT_TYPE", "CONTENT_LENGTH"):
            return self._request.META.get(meta_key)
        return self._request.META.get(f"HTTP_{meta_key}")

    def get_method(self) -> str:
        """Get HTTP method.

        Returns:
            HTTP method (GET, POST, etc.).
        """
        return self._request.method

    def get_path(self) -> str:
        """Get request path.

        Returns:
            Request path.
        """
        return self._request.path

    def get_url(self) -> str:
        """Get full request URL.

        Returns:
            Full URL string.
        """
        return self._request.build_absolute_uri()

    def get_accept_header(self) -> str:
        """Get Accept header.

        Returns:
            Accept header value.
        """
        return self._request.META.get("HTTP_ACCEPT", "")

    def get_user_agent(self) -> str:
        """Get User-Agent header.

        Returns:
            User-Agent header value.
        """
        return self._request.META.get("HTTP_USER_AGENT", "")

    def get_query_params(self) -> dict[str, str | list[str]]:
        """Get query parameters.

        Returns:
            Dict of query parameters.
        """
        return dict(self._request.GET)

    def get_query_param(self, name: str) -> str | None:
        """Get single query parameter.

        Args:
            name: Parameter name.

        Returns:
            Parameter value or None.
        """
        return self._request.GET.get(name)

    def get_body(self) -> Any:
        """Get request body.

        Returns:
            Parsed JSON body or None.
        """
        if not self._request.body:
            return None
        try:
            return json.loads(self._request.body)
        except (json.JSONDecodeError, TypeError):
            return None


# ============================================================================
# Response Helpers
# ============================================================================


def _json_error_response(
    status: int,
    detail: str,
    headers: dict[str, str] | None = None,
) -> JsonResponse:
    """Build a JSON error response.

    Args:
        status: HTTP status code.
        detail: Error detail message.
        headers: Optional extra headers.

    Returns:
        Django JsonResponse.
    """
    resp = JsonResponse({"error": detail}, status=status)
    if headers:
        for key, value in headers.items():
            resp[key] = value
    return resp


# ============================================================================
# Django Middleware Class
# ============================================================================


class X402PaymentMiddleware:
    """Django middleware for x402 payment handling.

    Django processes WSGI requests synchronously. This middleware wraps the
    Django WSGI application and intercepts requests to protected routes,
    verifying x402 payments before forwarding to the view.

    Payment info is attached to ``request.x402_payment_payload`` and
    ``request.x402_payment_requirements`` for downstream views.

    Example:
        ```python
        # settings.py
        MIDDLEWARE = [
            "x402.http.middleware.django.X402PaymentMiddleware",
            # ... other middleware
        ]

        X402_ROUTES = {
            "GET /api/weather/*": {
                "accepts": {
                    "scheme": "exact",
                    "payTo": "0x...",
                    "price": "$0.01",
                    "network": "eip155:84532",
                },
            },
        }
        ```
    """

    def __init__(self, get_response: Any = None) -> None:
        """Initialize Django payment middleware.

        Supports both Django new-style middleware (get_response parameter)
        and legacy WSGI-style initialization via Django settings.

        Args:
            get_response: Next middleware in the chain (Django new-style).
        """
        self._get_response = get_response

        # Load configuration from Django settings
        routes = getattr(settings, "X402_ROUTES", {})
        facilitator_url = getattr(settings, "X402_FACILITATOR_URL", "https://x402.org/facilitator")
        paywall_config_dict = getattr(settings, "X402_PAYWALL_CONFIG", None)

        # Build paywall config
        paywall_config = None
        if paywall_config_dict:
            paywall_config = PaywallConfig(
                app_name=paywall_config_dict.get("app_name"),
                app_logo=paywall_config_dict.get("app_logo"),
                testnet=paywall_config_dict.get("testnet", False),
                faucet_urls=paywall_config_dict.get("faucet_urls"),
            )

        # Create server
        from ...server import x402ResourceServerSync

        facilitator = getattr(settings, "X402_FACILITATOR_CLIENT", None)
        if facilitator is None:
            from ..facilitator_client import HTTPFacilitatorClientSync

            facilitator = HTTPFacilitatorClientSync(url=facilitator_url)

        server = x402ResourceServerSync(facilitator)

        # Register schemes from settings
        schemes = getattr(settings, "X402_SCHEMES", [])
        for registration in schemes:
            server.register(registration["network"], registration["server"])

        # Auto-register bazaar extension if routes declare it
        if _check_if_bazaar_needed(routes):
            _register_bazaar_extension(server)
            _validate_bazaar_extensions(routes)

        self._http_server = x402HTTPResourceServerSync(server, routes)
        self._paywall_config = paywall_config

        # Lazy initialization state
        self._sync_on_start = getattr(settings, "X402_SYNC_FACILITATOR_ON_START", True)
        self._init_done = False
        self._init_lock = threading.Lock()

        if self._sync_on_start:
            try:
                self._http_server.initialize()
                self._init_done = True
            except Exception as error:
                handle_background_init_error(error)

    def __call__(self, request: HttpRequest) -> Any:
        """Process request through x402 payment middleware.

        Args:
            request: Django HttpRequest.

        Returns:
            Django HttpResponse.
        """
        # Create adapter and context
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

        # Check if route requires payment
        if not self._http_server.requires_payment(context):
            return self._get_response(request)

        # Initialize on first protected request (double-checked locking)
        if self._sync_on_start and not self._init_done:
            with self._init_lock:
                if not self._init_done:
                    try:
                        self._http_server.initialize()
                    except FacilitatorResponseError as error:
                        return _json_error_response(502, str(error))
                    self._init_done = True

        # Process payment request synchronously
        try:
            result = self._http_server.process_http_request(context, self._paywall_config)
        except FacilitatorResponseError as error:
            return _json_error_response(502, str(error))
        except Exception:
            logger.exception("x402: unexpected error while processing an HTTP payment request")
            return _json_error_response(500, "Internal Server Error")

        if result.type == "no-payment-required":
            return self._get_response(request)

        if result.type == "payment-error":
            response = result.response
            if response is None:
                return _json_error_response(402, "Payment required")

            if response.is_html:
                return HttpResponse(
                    content=response.body,
                    status=response.status,
                    content_type="text/html; charset=utf-8",
                    headers=response.headers,
                )
            return JsonResponse(
                content=response.body or {},
                status=response.status,
                headers=response.headers,
            )

        if result.type == "payment-verified":
            # Store payment info on request for downstream views
            request.x402_payment_payload = result.payment_payload
            request.x402_payment_requirements = result.payment_requirements

            dispatcher = result.cancellation_dispatcher
            transport_context = HTTPTransportContext(request=context)

            # Call downstream
            try:
                response = self._get_response(request)
            except Exception as error:
                cancel_settlement = None
                if dispatcher is not None:
                    cancel_settlement = dispatcher.cancel_sync(
                        VerifiedPaymentCancelOptions(reason="handler_threw", error=error)
                    )
                failure_headers = self._http_server.create_failure_path_settlement_headers(
                    cancel_settlement,
                    result.before_handler_settlement,
                    result.payment_payload,
                )
                if not isinstance(failure_headers, dict) or not failure_headers:
                    raise
                return _json_error_response(500, "Internal Server Error", failure_headers)

            # Don't settle on error responses
            if response.status_code >= 400:
                cancel_settlement = None
                if dispatcher is not None:
                    cancel_settlement = dispatcher.cancel_sync(
                        VerifiedPaymentCancelOptions(
                            reason="handler_failed",
                            response_status=response.status_code,
                        )
                    )
                failure_headers = self._http_server.create_failure_path_settlement_headers(
                    cancel_settlement,
                    result.before_handler_settlement,
                    result.payment_payload,
                    response.get("Cache-Control"),
                )
                if isinstance(failure_headers, dict):
                    for key, value in failure_headers.items():
                        response[key] = value
                return response

            # Extract response body for buffering
            body = b""
            if hasattr(response, "content"):
                body = response.content
            elif hasattr(response, "streaming_content"):
                for chunk in response.streaming_content:
                    body += chunk

            # Extract and strip settlement overrides
            response_headers = dict(response.items())
            overrides = self._http_server._extract_settlement_overrides(response_headers)
            if overrides is not None:
                for key in list(response.keys()):
                    if key.lower() == SETTLEMENT_OVERRIDES_HEADER.lower():
                        del response[key]
                response_headers = {k: v for k, v in response_headers.items()
                                   if k.lower() != SETTLEMENT_OVERRIDES_HEADER.lower()}

            transport_context.response_headers = response_headers

            # Process settlement
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
                        return JsonResponse({}, status=402)
                    if settle_response.is_html:
                        return HttpResponse(
                            content=settle_response.body,
                            status=settle_response.status,
                            content_type="text/html; charset=utf-8",
                            headers=settle_response.headers,
                        )
                    return JsonResponse(
                        content=settle_response.body or {},
                        status=settle_response.status,
                        headers=settle_response.headers,
                    )

                # Add settlement headers
                headers = dict(response.items())
                headers.update(settle_result.headers)
                existing_cc = headers.get("Cache-Control")
                headers["Cache-Control"] = with_private_cache_control(existing_cc)

                return HttpResponse(
                    content=body,
                    status=response.status_code,
                    headers=headers,
                    content_type=response.get("Content-Type", "application/octet-stream"),
                )

            except FacilitatorResponseError as error:
                return _json_error_response(502, str(error))
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
                return JsonResponse(
                    {},
                    status=402,
                    headers={
                        "Cache-Control": PAYMENT_REQUIRED_CACHE_CONTROL,
                        **settle_headers,
                    },
                )

        # Fallthrough
        return self._get_response(request)


# ============================================================================
# Convenience Functions
# ============================================================================


def set_settlement_overrides(response: HttpResponse, overrides: dict[str, Any]) -> None:
    """Set settlement overrides on a Django response for partial settlement.

    The middleware extracts these before settlement and strips the header
    from the client response.

    Args:
        response: Django ``HttpResponse`` object.
        overrides: Settlement overrides, e.g. ``{"amount": "500"}``.
    """
    response[SETTLEMENT_OVERRIDES_HEADER] = json.dumps(overrides)


def payment_middleware(
    routes: RoutesConfig,
    server: x402ResourceServerSync,
    paywall_config: PaywallConfig | None = None,
    paywall_provider: PaywallProvider | None = None,
    sync_facilitator_on_start: bool = True,
) -> type:
    """Create a Django middleware class with pre-configured server.

    This is a factory function for cases where you want to pass the server
    directly instead of configuring via Django settings.

    Args:
        routes: Route configuration for protected endpoints.
        server: Pre-configured x402ResourceServerSync.
        paywall_config: Optional paywall UI configuration.
        paywall_provider: Optional custom paywall provider.
        sync_facilitator_on_start: Fetch facilitator support when middleware is created.

    Returns:
        Django middleware class.

    Example:
        ```python
        # urls.py or custom settings
        from x402 import x402ResourceServerSync
        from x402.http import HTTPFacilitatorClientSync
        from x402.http.middleware.django import payment_middleware

        facilitator = HTTPFacilitatorClientSync()
        server = x402ResourceServerSync(facilitator)

        routes = {
            "GET /api/weather/*": {
                "accepts": {
                    "scheme": "exact",
                    "payTo": "0x...",
                    "price": "$0.01",
                    "network": "eip155:84532",
                }
            }
        }

        X402PaymentMiddleware = payment_middleware(routes, server)
        # Add to MIDDLEWARE in settings.py
        ```
    """
    # Auto-register bazaar extension if routes declare it
    if _check_if_bazaar_needed(routes):
        _register_bazaar_extension(server)
        _validate_bazaar_extensions(routes)

    http_server = x402HTTPResourceServerSync(server, routes)

    if paywall_provider:
        http_server.register_paywall_provider(paywall_provider)

    init_done = False
    init_lock = threading.Lock()

    if sync_facilitator_on_start:
        try:
            http_server.initialize()
            init_done = True
        except Exception as error:
            handle_background_init_error(error)

    class ConfiguredX402Middleware:
        """Django middleware with pre-configured x402 server."""

        def __init__(self, get_response: Any = None) -> None:
            self._get_response = get_response

        def __call__(self, request: HttpRequest) -> Any:
            nonlocal init_done

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

            if not http_server.requires_payment(context):
                return self._get_response(request)

            if sync_facilitator_on_start and not init_done:
                with init_lock:
                    if not init_done:
                        try:
                            http_server.initialize()
                        except FacilitatorResponseError as error:
                            return _json_error_response(502, str(error))
                        init_done = True

            try:
                result = http_server.process_http_request(context, paywall_config)
            except FacilitatorResponseError as error:
                return _json_error_response(502, str(error))
            except Exception:
                logger.exception("x402: unexpected error while processing an HTTP payment request")
                return _json_error_response(500, "Internal Server Error")

            if result.type == "no-payment-required":
                return self._get_response(request)

            if result.type == "payment-error":
                response = result.response
                if response is None:
                    return _json_error_response(402, "Payment required")
                if response.is_html:
                    return HttpResponse(
                        content=response.body,
                        status=response.status,
                        content_type="text/html; charset=utf-8",
                        headers=response.headers,
                    )
                return JsonResponse(
                    content=response.body or {},
                    status=response.status,
                    headers=response.headers,
                )

            if result.type == "payment-verified":
                request.x402_payment_payload = result.payment_payload
                request.x402_payment_requirements = result.payment_requirements
                dispatcher = result.cancellation_dispatcher
                transport_context = HTTPTransportContext(request=context)

                try:
                    response = self._get_response(request)
                except Exception as error:
                    cancel_settlement = None
                    if dispatcher is not None:
                        cancel_settlement = dispatcher.cancel_sync(
                            VerifiedPaymentCancelOptions(reason="handler_threw", error=error)
                        )
                    failure_headers = http_server.create_failure_path_settlement_headers(
                        cancel_settlement,
                        result.before_handler_settlement,
                        result.payment_payload,
                    )
                    if not isinstance(failure_headers, dict) or not failure_headers:
                        raise
                    return _json_error_response(500, "Internal Server Error", failure_headers)

                if response.status_code >= 400:
                    cancel_settlement = None
                    if dispatcher is not None:
                        cancel_settlement = dispatcher.cancel_sync(
                            VerifiedPaymentCancelOptions(
                                reason="handler_failed",
                                response_status=response.status_code,
                            )
                        )
                    failure_headers = http_server.create_failure_path_settlement_headers(
                        cancel_settlement,
                        result.before_handler_settlement,
                        result.payment_payload,
                        response.get("Cache-Control"),
                    )
                    if isinstance(failure_headers, dict):
                        for key, value in failure_headers.items():
                            response[key] = value
                    return response

                body = b""
                if hasattr(response, "content"):
                    body = response.content
                elif hasattr(response, "streaming_content"):
                    for chunk in response.streaming_content:
                        body += chunk

                response_headers = dict(response.items())
                overrides = http_server._extract_settlement_overrides(response_headers)
                if overrides is not None:
                    for key in list(response.keys()):
                        if key.lower() == SETTLEMENT_OVERRIDES_HEADER.lower():
                            del response[key]
                    response_headers = {k: v for k, v in response_headers.items()
                                       if k.lower() != SETTLEMENT_OVERRIDES_HEADER.lower()}

                transport_context.response_headers = response_headers

                try:
                    settle_result = http_server.process_settlement(
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
                            return JsonResponse({}, status=402)
                        if settle_response.is_html:
                            return HttpResponse(
                                content=settle_response.body,
                                status=settle_response.status,
                                content_type="text/html; charset=utf-8",
                                headers=settle_response.headers,
                            )
                        return JsonResponse(
                            content=settle_response.body or {},
                            status=settle_response.status,
                            headers=settle_response.headers,
                        )

                    headers = dict(response.items())
                    headers.update(settle_result.headers)
                    existing_cc = headers.get("Cache-Control")
                    headers["Cache-Control"] = with_private_cache_control(existing_cc)

                    return HttpResponse(
                        content=body,
                        status=response.status_code,
                        headers=headers,
                        content_type=response.get("Content-Type", "application/octet-stream"),
                    )

                except FacilitatorResponseError as error:
                    return _json_error_response(502, str(error))
                except Exception:
                    logger.exception("x402: unexpected error while settling a verified payment")
                    settle_response = SettleResponse(
                        success=False,
                        error_reason="unexpected_settle_error",
                        error_message="unexpected error while settling the verified payment",
                        transaction="",
                        network=result.payment_requirements.network,
                    )
                    settle_headers = http_server._create_settlement_headers(
                        settle_response, result.payment_requirements
                    )
                    return JsonResponse(
                        {},
                        status=402,
                        headers={
                            "Cache-Control": PAYMENT_REQUIRED_CACHE_CONTROL,
                            **settle_headers,
                        },
                    )

            return self._get_response(request)

    return ConfiguredX402Middleware
