"""Edge case tests for MCP client module.

Covers error handling, timeouts, retries, malformed responses, and unusual
scenarios for both sync (x402MCPClientSync) and async (x402MCPClient) clients.

The sync client (x402MCPClientSync from client.py) has a simpler API:
  call_tool, call_tool_with_payment, client, payment_client.
The async client (x402MCPClient from client_async.py) adds hooks and probing:
  on_payment_required, on_before_payment, on_after_payment,
  get_tool_payment_requirements, call_tool_with_payment.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, Mock

import pytest

from x402.mcp import PaymentRequiredError, x402MCPClient, x402MCPClientSync
from x402.mcp.types import (
    MCPToolCallResult,
    MCPToolResult,
    PaymentRequiredHookResult,
)
from x402.mcp.utils import (
    _extract_payment_required_from_object,
    convert_mcp_result,
    extract_payment_required_from_error,
    extract_payment_required_from_result,
)
from x402.schemas import PaymentPayload, PaymentRequired


# ============================================================================
# Fixtures
# ============================================================================

VALID_PAYMENT_REQUIRED_JSON = (
    '{"x402Version":2,"accepts":[{"scheme":"exact","network":"eip155:84532",'
    '"amount":"1000","asset":"USDC","payTo":"0xrecipient","maxTimeoutSeconds":300}]}'
)

VALID_PAYMENT_REQUIRED_DICT = {
    "x402Version": 2,
    "accepts": [
        {
            "scheme": "exact",
            "network": "eip155:84532",
            "amount": "1000",
            "asset": "USDC",
            "payTo": "0xrecipient",
            "maxTimeoutSeconds": 300,
        }
    ],
}


class MockMCPResult:
    """Mock raw MCP result (has isError, matching real MCP SDK shape)."""

    def __init__(self, content=None, is_error=False, meta=None, structured_content=None):
        self.content = content or [{"type": "text", "text": "pong"}]
        self.isError = is_error
        self._meta = meta or {}
        self.structuredContent = structured_content


class MockMCPClient:
    """Mock MCP client (sync)."""

    def __init__(self):
        self.call_tool = Mock()
        self.connect = Mock()
        self.close = Mock()
        self.list_tools = Mock(return_value={"tools": []})


class MockPaymentClient:
    """Mock payment client (sync)."""

    def __init__(self):
        self.create_payment_payload = Mock()


class MockAsyncMCPClient:
    """Mock async MCP client."""

    def __init__(self):
        self.call_tool = AsyncMock()
        self.connect = AsyncMock()
        self.close = AsyncMock()
        self.list_tools = AsyncMock(return_value={"tools": []})


class MockAsyncPaymentClient:
    """Mock async payment client."""

    def __init__(self):
        self.create_payment_payload = AsyncMock()


def _mcp_tool_result(content=None, is_error=False, meta=None, structured_content=None):
    """Build an MCPToolResult (used by extract_payment_required_from_result)."""
    return MCPToolResult(
        content=content or [{"type": "text", "text": "pong"}],
        is_error=is_error,
        meta=meta or {},
        structured_content=structured_content,
    )


def _make_payment_required_result(text=None):
    """Build a raw MCP result with payment required data (for client call_tool)."""
    return MockMCPResult(
        content=[{"type": "text", "text": text or VALID_PAYMENT_REQUIRED_JSON}],
        is_error=True,
    )


def _make_paid_success_result():
    """Build a raw MCP result for a successful paid tool call."""
    return MockMCPResult(
        content=[{"type": "text", "text": "success"}],
        meta={
            "x402/payment-response": {
                "success": True,
                "transaction": "0xtx123",
                "network": "eip155:84532",
            }
        },
    )


PAYLOAD = PaymentPayload(
    x402_version=2,
    accepted={
        "scheme": "exact",
        "network": "eip155:84532",
        "amount": "1000",
        "asset": "USDC",
        "pay_to": "0xrecipient",
        "max_timeout_seconds": 300,
    },
    payload={"signature": "0x123"},
)


# ============================================================================
# 1. Malformed payment required responses
# ============================================================================


class TestMalformedPaymentRequiredResponses:
    """Tests for malformed isError=True responses that are not valid payment requests."""

    def test_is_error_with_invalid_json_text(self):
        """Tool returns isError=True but content text is not valid JSON."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": "not valid json at all {"}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_valid_json_no_accepts_key(self):
        """Tool returns isError=True with JSON but no 'accepts' key."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": '{"x402Version":2,"error":"insufficient funds"}'}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_empty_content_array(self):
        """Tool returns isError=True with empty content array."""
        result = _mcp_tool_result(content=[], is_error=True)
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_content_no_text_field(self):
        """Tool returns isError=True with content items that lack 'text' field."""
        result = _mcp_tool_result(
            content=[{"type": "image", "data": "base64data"}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_text_field_not_dict(self):
        """Content text parses to a list instead of a dict."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": "[1,2,3]"}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_empty_accepts_list(self):
        """JSON has accepts key but the list is empty."""
        data = {"x402Version": 2, "accepts": []}
        result = _mcp_tool_result(
            content=[{"type": "text", "text": json.dumps(data)}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_is_error_with_accepts_not_a_list(self):
        """JSON has accepts key but value is a string, not a list."""
        data = {"x402Version": 2, "accepts": "not_a_list"}
        result = _mcp_tool_result(
            content=[{"type": "text", "text": json.dumps(data)}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_structured_content_with_no_accepts(self):
        """structuredContent is present but lacks 'accepts' key."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": "error"}],
            is_error=True,
            structured_content={"x402Version": 2, "error": "no accepts"},
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_structured_content_empty_dict(self):
        """structuredContent is an empty dict."""
        result = _mcp_tool_result(
            content=[],
            is_error=True,
            structured_content={},
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_not_is_error_returns_none(self):
        """When is_error is False, extract_payment_required always returns None."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": VALID_PAYMENT_REQUIRED_JSON}],
            is_error=False,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None

    def test_sync_client_ignores_malformed_error_and_returns_raw(self):
        """Sync client returns isError result when payment JSON is malformed."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": "Server error: internal failure"}],
            is_error=True,
        )

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        result = client.call_tool("tool", {})

        assert result.payment_made is False
        assert result.is_error is True
        mock_payment.create_payment_payload.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_client_ignores_malformed_error_and_returns_raw(self):
        """Async client returns isError result when payment JSON is malformed."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": "Server error: internal failure"}],
            is_error=True,
        )

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        assert result.payment_made is False
        assert result.is_error is True
        mock_payment.create_payment_payload.assert_not_called()

    def test_fastmcp_wrapped_error_without_json(self):
        """FastMCP error wrapper text that doesn't contain JSON."""
        result = _mcp_tool_result(
            content=[{"type": "text", "text": "Error executing tool get_weather: something went wrong"}],
            is_error=True,
        )
        extracted = extract_payment_required_from_result(result)
        assert extracted is None


# ============================================================================
# 2. Network / connection errors
# ============================================================================


class TestNetworkConnectionErrors:
    """Tests for network and connection error scenarios."""

    @pytest.mark.asyncio
    async def test_async_client_connection_error_propagates(self):
        """Async client propagates ConnectionError from underlying MCP call."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()
        mock_mcp.call_tool.side_effect = ConnectionError("Connection refused")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(ConnectionError, match="Connection refused"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_client_timeout_error_propagates(self):
        """Async client propagates TimeoutError from underlying MCP call."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()
        mock_mcp.call_tool.side_effect = TimeoutError("Request timed out")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(TimeoutError, match="Request timed out"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_client_os_error_propagates(self):
        """Async client propagates OSError from underlying MCP call."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()
        mock_mcp.call_tool.side_effect = OSError("Network unreachable")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(OSError, match="Network unreachable"):
            await client.call_tool("tool", {})

    def test_sync_client_connection_error_propagates(self):
        """Sync client propagates ConnectionError from underlying MCP call."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()
        mock_mcp.call_tool.side_effect = ConnectionError("Connection refused")

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(ConnectionError, match="Connection refused"):
            client.call_tool("tool", {})

    def test_sync_client_timeout_error_propagates(self):
        """Sync client propagates TimeoutError from underlying MCP call."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()
        mock_mcp.call_tool.side_effect = TimeoutError("Request timed out")

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(TimeoutError, match="Request timed out"):
            client.call_tool("tool", {})

    def test_sync_client_os_error_propagates(self):
        """Sync client propagates OSError from underlying MCP call."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()
        mock_mcp.call_tool.side_effect = OSError("Network unreachable")

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(OSError, match="Network unreachable"):
            client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_client_generic_exception_propagates(self):
        """Async client propagates unexpected generic exceptions."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()
        mock_mcp.call_tool.side_effect = RuntimeError("Something unexpected")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(RuntimeError, match="Something unexpected"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_connection_error_during_retry(self):
        """Connection error occurs during the retry call (after payment creation)."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            ConnectionError("Lost during retry"),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(ConnectionError, match="Lost during retry"):
            await client.call_tool("tool", {})

    def test_sync_connection_error_during_retry(self):
        """Connection error occurs during the retry call (sync)."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            ConnectionError("Lost during retry"),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(ConnectionError, match="Lost during retry"):
            client.call_tool("tool", {})


# ============================================================================
# 3. Payment creation failures
# ============================================================================


class TestPaymentCreationFailures:
    """Tests for when payment payload creation fails."""

    @pytest.mark.asyncio
    async def test_async_create_payment_raises_exception(self):
        """Async client: create_payment_payload raises an exception."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.side_effect = RuntimeError("Insufficient balance")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(RuntimeError, match="Insufficient balance"):
            await client.call_tool("tool", {})

    def test_sync_create_payment_raises_exception(self):
        """Sync client: create_payment_payload raises an exception."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.side_effect = RuntimeError("Insufficient balance")

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(RuntimeError, match="Insufficient balance"):
            client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_create_payment_returns_none(self):
        """Async client: create_payment_payload returns None.

        When payment is required and create_payment_payload returns None,
        the code calls call_tool_with_payment which calls attach_payment_to_meta
        (which stores None as payload since it lacks model_dump), then retries.
        The retry MCP call returns the same payment-required result again since
        it's still isError=True, so extract_payment_required_from_result returns
        non-None and the flow enters the payment_required branch again with a
        different MCP result — but since there's no more side_effect, the mock
        reuses return_value. Since isError=True in the retry result, the client
        sees it as another payment-required and returns payment_made=False.
        """
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "retry result"}],
                is_error=False,
            ),
        ]
        mock_payment.create_payment_payload.return_value = None

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        # The retry completes; result depends on mock response
        assert isinstance(result, MCPToolCallResult)
        mock_payment.create_payment_payload.assert_called_once()

    def test_sync_create_payment_returns_none(self):
        """Sync client: create_payment_payload returns None.

        Sync client calls payload.model_dump() directly on the return value.
        None has no model_dump, so it stores None as the meta payload, then
        the retry MCP call proceeds. If mock returns non-error, payment_made=True.
        """
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "retry result"}],
                is_error=False,
            ),
        ]
        mock_payment.create_payment_payload.return_value = None

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        # Sync client calls payload.model_dump() — None has no model_dump → AttributeError
        with pytest.raises(AttributeError):
            client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_create_payment_returns_invalid_payload(self):
        """Async client: create_payment_payload returns a plain string.

        String has no __await__ so it's not awaited. attach_payment_to_meta
        stores it as-is (hasattr(str, 'model_dump') is False). The retry MCP
        call proceeds with the string in meta.
        """
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "retry result"}],
                is_error=False,
            ),
        ]
        mock_payment.create_payment_payload.return_value = "not a payment payload"

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        # Retry proceeds with string payload in meta
        assert isinstance(result, MCPToolCallResult)
        mock_payment.create_payment_payload.assert_called_once()

    def test_sync_create_payment_returns_invalid_payload(self):
        """Sync client: create_payment_payload returns a plain string.

        Sync client calls payload.model_dump() — string has no model_dump → AttributeError.
        """
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(content=[{"type": "text", "text": "retry"}], is_error=False),
        ]
        mock_payment.create_payment_payload.return_value = "not a payment payload"

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(AttributeError):
            client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_create_payment_timeout(self):
        """Async client: create_payment_payload times out."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.side_effect = TimeoutError("Payment signing timed out")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(TimeoutError, match="Payment signing timed out"):
            await client.call_tool("tool", {})

    def test_sync_create_payment_timeout(self):
        """Sync client: create_payment_payload times out."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.side_effect = TimeoutError("Signing timed out")

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(TimeoutError, match="Signing timed out"):
            client.call_tool("tool", {})


# ============================================================================
# 4. Settlement failures
# ============================================================================


class TestSettlementFailures:
    """Tests for settlement failure scenarios."""

    def test_sync_server_returns_success_false(self):
        """Server returns success=False in payment response."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        settle_response = {
            "success": False,
            "error_reason": "insufficient_funds",
            "error_message": "Not enough funds to settle",
            "transaction": "0xfailtx",
            "network": "eip155:84532",
        }

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "settlement failed"}],
                meta={"x402/payment-response": settle_response},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        result = client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is not None
        assert result.payment_response.success is False
        assert result.payment_response.error_reason == "insufficient_funds"

    @pytest.mark.asyncio
    async def test_async_server_returns_success_false(self):
        """Async: server returns success=False in payment response."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        settle_response = {
            "success": False,
            "error_reason": "network_congestion",
            "error_message": "Try again later",
            "transaction": "0xfailtx",
            "network": "eip155:84532",
        }

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "settlement failed"}],
                meta={"x402/payment-response": settle_response},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is not None
        assert result.payment_response.success is False
        assert result.payment_response.error_reason == "network_congestion"

    def test_sync_server_returns_empty_payment_response(self):
        """Server returns empty dict in payment-response meta (validation fails → None)."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"x402/payment-response": {}},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        result = client.call_tool("tool", {})

        assert result.payment_made is True
        # Empty dict fails SettleResponse validation → payment_response is None
        assert result.payment_response is None

    @pytest.mark.asyncio
    async def test_async_server_returns_empty_payment_response(self):
        """Async: server returns empty dict in payment-response meta."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"x402/payment-response": {}},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is None

    def test_sync_server_returns_missing_fields_in_response(self):
        """Server returns payment response with missing required fields (→ None)."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"x402/payment-response": {"success": True}},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        result = client.call_tool("tool", {})

        assert result.payment_made is True
        # Missing transaction/network fails SettleResponse validation → None
        assert result.payment_response is None

    @pytest.mark.asyncio
    async def test_async_server_returns_missing_fields_in_response(self):
        """Async: server returns payment response with missing required fields."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"x402/payment-response": {"success": True}},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is None

    def test_sync_no_payment_response_meta(self):
        """Retry succeeds but meta contains no payment-response key."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"some_other_key": "value"},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)
        result = client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is None

    @pytest.mark.asyncio
    async def test_async_no_payment_response_meta(self):
        """Async: retry succeeds but meta contains no payment-response key."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(
                content=[{"type": "text", "text": "ok"}],
                meta={"some_other_key": "value"},
            ),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})

        assert result.payment_made is True
        assert result.payment_response is None


# ============================================================================
# 5. Concurrent access
# ============================================================================


class TestConcurrentAccess:
    """Tests for concurrent access and thread safety."""

    @pytest.mark.asyncio
    async def test_concurrent_async_calls_same_payment_required(self):
        """Multiple concurrent async calls with the same payment required."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        payment_required_result = _make_payment_required_result()
        success_result = MockMCPResult(
            content=[{"type": "text", "text": "ok"}],
        )

        call_count = 0

        async def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 5:
                return payment_required_result
            return success_result

        mock_mcp.call_tool.side_effect = side_effect
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        tasks = [client.call_tool(f"tool_{i}", {"i": i}) for i in range(5)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            assert isinstance(r, MCPToolCallResult)

    @pytest.mark.asyncio
    async def test_async_concurrent_payment_required_then_free_tool(self):
        """Mix of concurrent paid and free tool calls."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        paid_count = 0

        async def side_effect(params, **kwargs):
            nonlocal paid_count
            name = params.get("name", "unknown")
            if name.startswith("paid"):
                paid_count += 1
                if paid_count % 2 == 1:
                    return _make_payment_required_result()
                return _make_paid_success_result()
            return MockMCPResult(content=[{"type": "text", "text": "free"}])

        mock_mcp.call_tool.side_effect = side_effect
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        tasks = []
        for i in range(10):
            name = f"paid_tool_{i}" if i % 2 == 0 else f"free_tool_{i}"
            tasks.append(client.call_tool(name, {}))

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            assert isinstance(r, MCPToolCallResult)

    def test_sync_sequential_calls_independent(self):
        """Sequential sync calls are independent (no shared state leakage)."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        # 4 calls: free1(1) + paid_initial(2) + paid_retry(3) + free2(4)
        mock_mcp.call_tool.side_effect = [
            MockMCPResult(content=[{"type": "text", "text": "free1"}]),
            _make_payment_required_result(),
            MockMCPResult(content=[{"type": "text", "text": "paid success"}]),
            MockMCPResult(content=[{"type": "text", "text": "free2"}]),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=True)

        r1 = client.call_tool("free_tool", {})
        r2 = client.call_tool("paid_tool", {})
        r3 = client.call_tool("free_tool2", {})

        assert r1.payment_made is False
        assert r2.payment_made is True
        assert r3.payment_made is False


# ============================================================================
# 6. get_tool_payment_requirements edge cases (async client only)
# ============================================================================


class TestGetToolPaymentRequirements:
    """Tests for get_tool_payment_requirements edge cases (async client only)."""

    @pytest.mark.asyncio
    async def test_async_tool_returns_no_payment_requirements(self):
        """Tool returns success (not an error) — free tool, no requirements."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": "no payment needed"}],
            is_error=False,
        )

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.get_tool_payment_requirements("free_tool", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_async_tool_raises_exception_during_probe(self):
        """Tool raises an exception when probing for payment requirements."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = RuntimeError("Tool probe failed")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(RuntimeError, match="Tool probe failed"):
            await client.get_tool_payment_requirements("broken_tool", {})

    @pytest.mark.asyncio
    async def test_async_tool_returns_partial_payment_requirements(self):
        """Tool returns error with partial/invalid payment requirements JSON."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": '{"x402Version":2}'}],
            is_error=True,
        )

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.get_tool_payment_requirements("partial_tool", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_async_tool_returns_empty_error_content(self):
        """Tool returns isError=True but with empty content."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(content=[], is_error=True)

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.get_tool_payment_requirements("empty_error_tool", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_async_probe_with_structured_content(self):
        """Probe finds payment requirements in structuredContent."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        pr_dict = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "amount": "500",
                    "asset": "USDC",
                    "payTo": "0xaddr",
                    "maxTimeoutSeconds": 600,
                }
            ],
        }

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": "Payment required"}],
            is_error=True,
            structured_content=pr_dict,
        )

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.get_tool_payment_requirements("structured_tool", {})

        assert result is not None
        assert result.x402_version == 2
        assert len(result.accepts) == 1
        assert result.accepts[0].scheme == "exact"

    @pytest.mark.asyncio
    async def test_async_probe_connection_error(self):
        """Probe fails with connection error."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = ConnectionError("Cannot connect")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        with pytest.raises(ConnectionError, match="Cannot connect"):
            await client.get_tool_payment_requirements("offline_tool", {})


# ============================================================================
# 7. Hook edge cases (async client only)
# ============================================================================


class TestHookEdgeCases:
    """Tests for hook edge cases (x402MCPClient async only)."""

    @pytest.mark.asyncio
    async def test_async_payment_required_hook_raises_exception(self):
        """Payment required hook raises an exception."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.return_value = PAYLOAD

        def bad_hook(ctx):
            raise RuntimeError("Hook failed")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_payment_required(bad_hook)

        with pytest.raises(RuntimeError, match="Hook failed"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_before_payment_hook_raises_exception(self):
        """Before payment hook raises an exception."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            MockMCPResult(content=[{"type": "text", "text": "ok"}]),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        def bad_before_hook(ctx):
            raise RuntimeError("Before hook failed")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_before_payment(bad_before_hook)

        with pytest.raises(RuntimeError, match="Before hook failed"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_after_payment_hook_raises_exception(self):
        """After payment hook raises an exception — payment still completed."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            _make_paid_success_result(),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        def bad_after_hook(ctx):
            raise RuntimeError("After hook failed")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_after_payment(bad_after_hook)

        with pytest.raises(RuntimeError, match="After hook failed"):
            await client.call_tool("tool", {})

    @pytest.mark.asyncio
    async def test_async_multiple_hooks_some_fail(self):
        """Multiple payment_required hooks registered, first one fails."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()

        hook_calls = []

        def failing_hook(ctx):
            hook_calls.append("failing")
            raise RuntimeError("Hook 1 failed")

        def second_hook(ctx):
            hook_calls.append("second")
            return PaymentRequiredHookResult(abort=True)

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_payment_required(failing_hook)
        client.on_payment_required(second_hook)

        with pytest.raises(RuntimeError, match="Hook 1 failed"):
            await client.call_tool("tool", {})

        # Only the first hook was called before it raised
        assert hook_calls == ["failing"]

    @pytest.mark.asyncio
    async def test_async_payment_required_hook_modifies_context(self):
        """Payment required hook modifies the context object."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()
        mock_payment.create_payment_payload.return_value = PAYLOAD

        seen_args = []

        def observing_hook(ctx):
            seen_args.append(ctx.arguments)
            ctx.arguments["observed"] = True
            return PaymentRequiredHookResult()

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_payment_required(observing_hook)

        await client.call_tool("tool", {"original": "arg"})

        assert len(seen_args) == 1
        assert seen_args[0]["original"] == "arg"
        assert seen_args[0]["observed"] is True

    @pytest.mark.asyncio
    async def test_async_after_hook_receives_correct_context(self):
        """After payment hook receives correct context fields."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            _make_paid_success_result(),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        received_contexts = []

        def after_hook(ctx):
            received_contexts.append(ctx)

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_after_payment(after_hook)

        await client.call_tool("my_tool", {"arg": "val"})

        assert len(received_contexts) == 1
        ctx = received_contexts[0]
        assert ctx.tool_name == "my_tool"
        assert ctx.payment_payload is PAYLOAD
        assert ctx.result is not None
        assert ctx.settle_response is not None

    @pytest.mark.asyncio
    async def test_async_hook_returns_none_is_treated_as_falsy(self):
        """Hook returning None is treated as falsy (no abort, no custom payment)."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            _make_paid_success_result(),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        def noop_hook(ctx):
            return None

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_payment_required(noop_hook)

        result = await client.call_tool("tool", {})
        assert result.payment_made is True

    @pytest.mark.asyncio
    async def test_async_before_and_after_hooks_both_called(self):
        """Both before and after hooks are called in sequence."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.side_effect = [
            _make_payment_required_result(),
            _make_paid_success_result(),
        ]
        mock_payment.create_payment_payload.return_value = PAYLOAD

        call_log = []

        def before_hook(ctx):
            call_log.append("before")

        def after_hook(ctx):
            call_log.append("after")

        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        client.on_before_payment(before_hook)
        client.on_after_payment(after_hook)

        await client.call_tool("tool", {})

        assert call_log == ["before", "after"]

    @pytest.mark.asyncio
    async def test_async_on_payment_requested_denies(self):
        """on_payment_requested callback denies payment."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()

        client = x402MCPClient(
            mock_mcp,
            mock_payment,
            auto_payment=True,
            on_payment_requested=lambda ctx: False,
        )

        with pytest.raises(PaymentRequiredError):
            await client.call_tool("tool", {})

        mock_payment.create_payment_payload.assert_not_called()


# ============================================================================
# 8. convert_mcp_result edge cases
# ============================================================================


class TestConvertMcpResultEdgeCases:
    """Tests for convert_mcp_result with unusual input shapes."""

    def test_object_with_no_spec(self):
        """convert_mcp_result handles object with only desired attrs set."""
        obj = Mock(spec=[])
        obj.content = []
        obj.isError = False
        obj._meta = {}
        obj.structuredContent = None

        result = convert_mcp_result(obj)
        assert result.is_error is False
        assert result.content == []

    def test_non_dict_meta(self):
        """convert_mcp_result handles meta that is not a dict."""
        obj = Mock(spec=[])
        obj.content = [{"type": "text", "text": "ok"}]
        obj.isError = False
        obj._meta = "not_a_dict"
        obj.structuredContent = None

        result = convert_mcp_result(obj)
        assert result.meta == {}

    def test_none_content(self):
        """convert_mcp_result handles None content."""
        obj = Mock(spec=[])
        obj.content = None
        obj.isError = False
        obj._meta = {}
        obj.structuredContent = None

        result = convert_mcp_result(obj)
        assert result.content == []

    def test_is_error_fallback_to_snake_case(self):
        """convert_mcp_result falls back to is_error when isError is absent."""
        obj = Mock(spec=[])
        obj.content = []
        obj._meta = {}
        obj.structuredContent = None
        del obj.isError
        obj.is_error = True

        result = convert_mcp_result(obj)
        assert result.is_error is True

    def test_no_error_attributes_defaults_false(self):
        """convert_mcp_result defaults is_error to False when neither attr exists."""
        obj = Mock(spec=[])
        obj.content = []
        obj._meta = {}
        obj.structuredContent = None
        del obj.isError
        del obj.is_error

        result = convert_mcp_result(obj)
        assert result.is_error is False

    def test_structured_content_passed_through(self):
        """convert_mcp_result passes structuredContent through."""
        sc = {"x402Version": 2, "accepts": []}
        obj = Mock(spec=[])
        obj.content = []
        obj.isError = True
        obj._meta = {}
        obj.structuredContent = sc

        result = convert_mcp_result(obj)
        assert result.structured_content == sc

    def test_content_with_non_list_value(self):
        """convert_mcp_result handles content that is not a list."""
        obj = Mock(spec=[])
        obj.content = "not_a_list"
        obj.isError = False
        obj._meta = {}
        obj.structuredContent = None

        result = convert_mcp_result(obj)
        assert result.content == []


# ============================================================================
# 9. extract_payment_required_from_error edge cases
# ============================================================================


class TestExtractPaymentRequiredFromError:
    """Tests for extract_payment_required_from_error utility."""

    def test_valid_402_error_with_data(self):
        """Extract payment required from a valid 402 JSON-RPC error."""
        error = {
            "code": 402,
            "data": {
                "x402Version": 2,
                "accepts": [
                    {
                        "scheme": "exact",
                        "network": "eip155:84532",
                        "amount": "1000",
                        "asset": "USDC",
                        "payTo": "0xaddr",
                        "maxTimeoutSeconds": 300,
                    }
                ],
            },
        }
        result = extract_payment_required_from_error(error)
        assert result is not None
        assert result.x402_version == 2

    def test_non_dict_error(self):
        """Non-dict error returns None."""
        assert extract_payment_required_from_error("not a dict") is None
        assert extract_payment_required_from_error(None) is None
        assert extract_payment_required_from_error(42) is None

    def test_non_402_code(self):
        """Error with non-402 code returns None."""
        error = {"code": 500, "data": {"x402Version": 2, "accepts": []}}
        assert extract_payment_required_from_error(error) is None

    def test_402_with_no_data(self):
        """402 error with no data field returns None."""
        error = {"code": 402}
        assert extract_payment_required_from_error(error) is None

    def test_402_with_non_object_data(self):
        """402 error with data that is not a dict returns None."""
        error = {"code": 402, "data": "not_an_object"}
        assert extract_payment_required_from_error(error) is None

    def test_402_with_empty_data(self):
        """402 error with empty data dict returns None."""
        error = {"code": 402, "data": {}}
        assert extract_payment_required_from_error(error) is None


# ============================================================================
# 10. _extract_payment_required_from_object edge cases
# ============================================================================


class TestExtractPaymentRequiredFromObject:
    """Tests for _extract_payment_required_from_object utility."""

    def test_no_x402_version(self):
        """Object without x402Version returns None."""
        assert _extract_payment_required_from_object({"accepts": []}) is None

    def test_no_accepts(self):
        """Object with x402Version but no accepts returns None."""
        assert _extract_payment_required_from_object({"x402Version": 2}) is None

    def test_accepts_empty_list(self):
        """Object with empty accepts list returns None."""
        assert _extract_payment_required_from_object({"x402Version": 2, "accepts": []}) is None

    def test_invalid_accepts_item(self):
        """Object with malformed accepts item returns None."""
        obj = {"x402Version": 2, "accepts": [{"scheme": None, "network": None}]}
        result = _extract_payment_required_from_object(obj)
        assert result is None

    def test_snake_case_x402_version(self):
        """Object with x402_version (snake_case) is handled."""
        obj = {
            "x402_version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "amount": "100",
                    "asset": "USDC",
                    "payTo": "0xaddr",
                    "maxTimeoutSeconds": 120,
                }
            ],
        }
        result = _extract_payment_required_from_object(obj)
        assert result is not None
        assert result.x402_version == 2

    def test_none_version_defaults_to_2(self):
        """Object with accepts but x402Version is None defaults to 2."""
        obj = {
            "x402Version": None,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "amount": "100",
                    "asset": "USDC",
                    "payTo": "0xaddr",
                    "maxTimeoutSeconds": 120,
                }
            ],
        }
        result = _extract_payment_required_from_object(obj)
        assert result is not None
        assert result.x402_version == 2

    def test_valid_v2_object(self):
        """Valid v2 payment required object."""
        result = _extract_payment_required_from_object(VALID_PAYMENT_REQUIRED_DICT)
        assert result is not None
        assert result.x402_version == 2
        assert len(result.accepts) == 1

    def test_malformed_accepts_missing_required_fields(self):
        """Accepts item with missing required fields returns None."""
        obj = {
            "x402Version": 2,
            "accepts": [
                {
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "amount": "100",
                    "asset": "USDC",
                    "payTo": "0xaddr",
                    # maxTimeoutSeconds missing
                }
            ],
        }
        result = _extract_payment_required_from_object(obj)
        assert result is None


# ============================================================================
# 11. MCPToolCallResult defaults
# ============================================================================


class TestMCPToolCallResultDefaults:
    """Tests for MCPToolCallResult default values."""

    def test_default_values(self):
        """MCPToolCallResult has correct defaults when constructed manually."""
        r = MCPToolCallResult(
            content=[],
            is_error=False,
            payment_response=None,
            payment_made=False,
        )
        assert r.content == []
        assert r.is_error is False
        assert r.payment_response is None
        assert r.payment_made is False

    def test_explicit_values(self):
        """MCPToolCallResult accepts explicit values."""
        r = MCPToolCallResult(
            content=[{"type": "text", "text": "hi"}],
            is_error=True,
            payment_made=True,
        )
        assert len(r.content) == 1
        assert r.is_error is True
        assert r.payment_made is True

    def test_partial_explicit(self):
        """MCPToolCallResult accepts partial explicit values."""
        r = MCPToolCallResult(
            content=[],
            payment_made=True,
        )
        assert r.content == []
        assert r.payment_made is True
        assert r.is_error is False
        assert r.payment_response is None


# ============================================================================
# 12. PaymentRequiredError edge cases
# ============================================================================


class TestPaymentRequiredError:
    """Tests for PaymentRequiredError edge cases."""

    def test_error_attributes(self):
        """PaymentRequiredError has correct attributes."""
        pr = PaymentRequired(x402_version=2, accepts=[])
        err = PaymentRequiredError("test message", pr)
        assert err.code == 402
        assert err.payment_required is pr
        assert str(err) == "test message"

    def test_error_without_payment_required(self):
        """PaymentRequiredError can be created without payment_required."""
        err = PaymentRequiredError("no payment info")
        assert err.code == 402
        assert err.payment_required is None

    def test_error_is_exception(self):
        """PaymentRequiredError is an Exception subclass."""
        err = PaymentRequiredError("msg")
        assert isinstance(err, Exception)


# ============================================================================
# 13. x402MCPSession._extract_payment_required edge cases
# ============================================================================


class TestX402MCPSessionExtractPaymentRequired:
    """Tests for the legacy x402MCPSession._extract_payment_required method."""

    def _make_session(self):
        """Create a minimal x402MCPSession for testing."""
        from x402.mcp.client import x402MCPSession

        mock_session = Mock()
        mock_x402_client = Mock()
        return x402MCPSession(mock_session, mock_x402_client, auto_payment=True)

    def test_structured_content_with_accepts(self):
        """Extract from structuredContent that has accepts."""
        session = self._make_session()

        class R:
            structuredContent = VALID_PAYMENT_REQUIRED_DICT
            content = []

        result = session._extract_payment_required(R())
        assert result is not None

    def test_structured_content_without_accepts(self):
        """structuredContent present but no accepts key."""
        session = self._make_session()

        class R:
            structuredContent = {"error": "something"}
            content = []

        result = session._extract_payment_required(R())
        assert result is None

    def test_content_text_with_json_accepts(self):
        """Extract from content[0].text JSON with accepts."""
        session = self._make_session()

        class R:
            structuredContent = None
            content = [type("Item", (), {"text": VALID_PAYMENT_REQUIRED_JSON})()]

        result = session._extract_payment_required(R())
        assert result is not None

    def test_content_text_without_json(self):
        """Content text is not JSON."""
        session = self._make_session()

        class R:
            structuredContent = None
            content = [type("Item", (), {"text": "plain text"})()]

        result = session._extract_payment_required(R())
        assert result is None

    def test_content_item_without_text(self):
        """Content items have no text attribute."""
        session = self._make_session()

        class R:
            structuredContent = None
            content = [type("Item", (), {"data": "base64"})()]

        result = session._extract_payment_required(R())
        assert result is None

    def test_no_content_attribute(self):
        """Result has no content attribute at all."""
        session = self._make_session()

        class R:
            structuredContent = None

        result = session._extract_payment_required(R())
        assert result is None

    def test_empty_content(self):
        """Result has empty content list."""
        session = self._make_session()

        class R:
            structuredContent = None
            content = []

        result = session._extract_payment_required(R())
        assert result is None

    def test_fastmcp_wrapped_error_json(self):
        """FastMCP wrapped error format with embedded JSON."""
        session = self._make_session()

        fastmcp_text = (
            'Error executing tool get_weather: {"x402Version":2,'
            '"accepts":[{"scheme":"exact","network":"eip155:84532",'
            '"amount":"500","asset":"USDC","payTo":"0xaddr",'
            '"maxTimeoutSeconds":120}]}'
        )

        class R:
            structuredContent = None
            content = [type("Item", (), {"text": fastmcp_text})()]

        result = session._extract_payment_required(R())
        assert result is not None


# ============================================================================
# 14. _try_extract_payment_json edge cases
# ============================================================================


class TestTryExtractPaymentJson:
    """Tests for _try_extract_payment_json utility."""

    def test_valid_json_with_accepts(self):
        """Valid JSON with accepts key."""
        from x402.mcp.client import _try_extract_payment_json

        result = _try_extract_payment_json(VALID_PAYMENT_REQUIRED_JSON)
        assert result is not None
        assert "accepts" in result

    def test_invalid_json(self):
        """Invalid JSON string."""
        from x402.mcp.client import _try_extract_payment_json

        result = _try_extract_payment_json("not json {{{")
        assert result is None

    def test_valid_json_without_accepts(self):
        """Valid JSON without accepts key."""
        from x402.mcp.client import _try_extract_payment_json

        result = _try_extract_payment_json('{"x402Version":2}')
        assert result is None

    def test_fastmcp_wrapped_format(self):
        """FastMCP wrapped error format."""
        from x402.mcp.client import _try_extract_payment_json

        text = (
            'Error executing tool get_weather: {"x402Version":2,'
            '"accepts":[{"scheme":"exact","network":"eip155:84532",'
            '"amount":"100","asset":"USDC","payTo":"0xaddr",'
            '"maxTimeoutSeconds":300}]}'
        )
        result = _try_extract_payment_json(text)
        assert result is not None
        assert "accepts" in result

    def test_fastmcp_format_no_accepts(self):
        """FastMCP wrapped format but no accepts in JSON."""
        from x402.mcp.client import _try_extract_payment_json

        text = 'Error executing tool: {"x402Version":2,"error":"no accepts"}'
        result = _try_extract_payment_json(text)
        assert result is None

    def test_empty_string(self):
        """Empty string input."""
        from x402.mcp.client import _try_extract_payment_json

        result = _try_extract_payment_json("")
        assert result is None

    def test_json_array_extracts_inner_accepts(self):
        """JSON array containing a dict with accepts — regex extracts inner dict."""
        from x402.mcp.client import _try_extract_payment_json

        result = _try_extract_payment_json('[{"accepts":[]}]')
        # The regex matches {"accepts":[]} and returns it
        assert result is not None
        assert "accepts" in result

    def test_nested_wrapper_json_returns_none(self):
        """JSON with accepts nested under a wrapper key — not top-level."""
        from x402.mcp.client import _try_extract_payment_json

        text = '{"wrapper":{"x402Version":2,"accepts":[{"scheme":"exact","network":"eip155:84532","amount":"50","asset":"USDC","payTo":"0x1","maxTimeoutSeconds":60}]}}'
        result = _try_extract_payment_json(text)
        # Regex matches the whole string but parsed dict has "wrapper" not "accepts" at top
        assert result is None


# ============================================================================
# 15. create_payment_required_error
# ============================================================================


class TestCreatePaymentRequiredError:
    """Tests for create_payment_required_error utility."""

    def test_creates_error_with_custom_message(self):
        from x402.mcp.utils import create_payment_required_error

        pr = PaymentRequired(x402_version=2, accepts=[])
        err = create_payment_required_error(pr, "custom msg")
        assert str(err) == "custom msg"
        assert err.payment_required is pr

    def test_creates_error_with_default_message(self):
        from x402.mcp.utils import create_payment_required_error

        pr = PaymentRequired(x402_version=2, accepts=[])
        err = create_payment_required_error(pr)
        assert str(err) == "Payment required"
        assert err.code == 402


# ============================================================================
# 16. is_payment_required_error
# ============================================================================


class TestIsPaymentRequiredError:
    """Tests for is_payment_required_error utility."""

    def test_returns_true_for_payment_required_error(self):
        from x402.mcp.utils import is_payment_required_error

        err = PaymentRequiredError("test")
        assert is_payment_required_error(err) is True

    def test_returns_false_for_generic_exception(self):
        from x402.mcp.utils import is_payment_required_error

        assert is_payment_required_error(RuntimeError("test")) is False

    def test_returns_false_for_non_exception(self):
        from x402.mcp.utils import is_payment_required_error

        assert is_payment_required_error("not an exception") is False


# ============================================================================
# 17. Auto-payment disabled warnings
# ============================================================================


class TestAutoPaymentDisabled:
    """Tests for auto_payment=False behavior."""

    @pytest.mark.asyncio
    async def test_async_no_auto_payment_no_hook_raises(self):
        """Async client with auto_payment=False and no hooks raises PaymentRequiredError."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()

        with pytest.warns(UserWarning, match="auto_payment=False"):
            client = x402MCPClient(
                mock_mcp, mock_payment, auto_payment=False, on_payment_requested=None
            )

        with pytest.raises(PaymentRequiredError) as exc_info:
            await client.call_tool("tool", {})

        assert exc_info.value.code == 402

    @pytest.mark.asyncio
    async def test_async_auto_payment_true_no_warning(self):
        """Async client with auto_payment=True does not warn."""
        mock_mcp = MockAsyncMCPClient()
        mock_payment = MockAsyncPaymentClient()

        mock_mcp.call_tool.return_value = MockMCPResult(
            content=[{"type": "text", "text": "ok"}],
            is_error=False,
        )

        # Should not warn
        client = x402MCPClient(mock_mcp, mock_payment, auto_payment=True)
        result = await client.call_tool("tool", {})
        assert result.payment_made is False

    def test_sync_auto_payment_false_no_payment(self):
        """Sync client with auto_payment=False does not create payment."""
        mock_mcp = MockMCPClient()
        mock_payment = MockPaymentClient()

        mock_mcp.call_tool.return_value = _make_payment_required_result()

        client = x402MCPClientSync(mock_mcp, mock_payment, auto_payment=False)
        result = client.call_tool("tool", {})

        assert result.payment_made is False
        assert result.is_error is True
        mock_payment.create_payment_payload.assert_not_called()
