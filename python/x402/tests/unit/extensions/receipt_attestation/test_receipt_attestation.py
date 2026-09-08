"""Tests for the Receipt Attestation Extension."""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from x402.extensions.receipt_attestation import (
    RECEIPT_ATTESTATION,
    RECEIPT_VERSION,
    AttestationData,
    InMemoryReceiptStorage,
    ReceiptAttestationResourceServerExtension,
    ReceiptData,
    ReceiptGenerator,
    ReceiptInfo,
    ReceiptPayloadModel,
    ReceiptVerifier,
    SignedReceiptModel,
    extract_receipt_from_extensions,
    is_receipt_attestation_extension,
    receipt_attestation_schema,
    receipt_attestation_server_extension,
    receipt_payload_schema,
)

# ============================================================================
# Constants
# ============================================================================


class TestConstants:
    def test_receipt_attestation_key(self) -> None:
        assert RECEIPT_ATTESTATION == "receipt-attestation"

    def test_receipt_version(self) -> None:
        assert RECEIPT_VERSION == 1


# ============================================================================
# Data Classes
# ============================================================================


class TestReceiptData:
    def test_default_values(self) -> None:
        r = ReceiptData()
        assert r.version == RECEIPT_VERSION
        assert r.network == ""
        assert r.resource_url == ""
        assert r.amount == ""
        assert r.asset == ""
        assert r.pay_to == ""
        assert r.payer == ""
        assert r.issued_at == 0
        assert r.transaction == ""
        assert r.proof_hash == ""

    def test_custom_values(self) -> None:
        r = ReceiptData(
            version=1,
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            issued_at=1703123456,
            transaction="0x1234",
            proof_hash="0xabcd",
        )
        assert r.network == "eip155:8453"
        assert r.amount == "10000"
        assert r.transaction == "0x1234"


class TestAttestationData:
    def test_default_values(self) -> None:
        a = AttestationData()
        assert a.receipt == {}
        assert a.attester == ""
        assert a.issued_at == 0
        assert a.expires_at == 0
        assert a.metadata == {}


# ============================================================================
# Pydantic Models
# ============================================================================


class TestReceiptPayloadModel:
    def test_to_eip712_dict(self) -> None:
        payload = ReceiptPayloadModel(
            version=1,
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            issued_at=1703123456,
        )
        d = payload.to_eip712_dict()
        assert d["version"] == 1
        assert d["network"] == "eip155:8453"
        assert d["resourceUrl"] == "https://api.example.com/data"
        assert d["amount"] == "10000"
        assert d["payTo"] == "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"
        assert d["issuedAt"] == 1703123456

    def test_to_json(self) -> None:
        payload = ReceiptPayloadModel(
            version=1,
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            issued_at=1703123456,
        )
        j = payload.to_json()
        parsed = json.loads(j)
        assert parsed["network"] == "eip155:8453"

    def test_optional_fields(self) -> None:
        payload = ReceiptPayloadModel(
            version=1,
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            issued_at=1703123456,
            transaction="0x1234",
            proof_hash="0xabcd",
        )
        d = payload.to_eip712_dict()
        assert d["transaction"] == "0x1234"
        assert d["proofHash"] == "0xabcd"


class TestSignedReceiptModel:
    def test_eip712_format(self) -> None:
        sr = SignedReceiptModel(
            format="eip712",
            payload=ReceiptPayloadModel(
                version=1,
                network="eip155:8453",
                resource_url="https://api.example.com/data",
                amount="10000",
                asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
                payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
                issued_at=1703123456,
            ),
            signature="0xabcdef",
        )
        assert sr.format == "eip712"
        assert sr.payload is not None
        assert sr.signature == "0xabcdef"

    def test_jws_format(self) -> None:
        sr = SignedReceiptModel(
            format="jws",
            payload=None,
            signature="header.payload.signature",
        )
        assert sr.format == "jws"
        assert sr.payload is None


class TestReceiptInfo:
    def test_default(self) -> None:
        info = ReceiptInfo()
        assert info.enabled is True
        assert info.supported_formats == ["eip712"]

    def test_custom(self) -> None:
        info = ReceiptInfo(enabled=False, supported_formats=["jws"])
        assert info.enabled is False
        assert info.supported_formats == ["jws"]


# ============================================================================
# Storage
# ============================================================================


class TestInMemoryReceiptStorage:
    @pytest.mark.asyncio
    async def test_store_and_retrieve(self) -> None:
        storage = InMemoryReceiptStorage()
        receipt = {"id": "abc", "network": "eip155:8453"}
        await storage.store("abc", receipt)
        result = await storage.retrieve("abc")
        assert result is not None
        assert result["id"] == "abc"

    @pytest.mark.asyncio
    async def test_retrieve_nonexistent(self) -> None:
        storage = InMemoryReceiptStorage()
        result = await storage.retrieve("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_by_payer(self) -> None:
        storage = InMemoryReceiptStorage()
        await storage.store("1", {"id": "1", "payer": "0xA"})
        await storage.store("2", {"id": "2", "payer": "0xB"})
        await storage.store("3", {"id": "3", "payer": "0xA"})

        receipts = await storage.list_by_payer("0xA")
        assert len(receipts) == 2
        ids = {r["id"] for r in receipts}
        assert ids == {"1", "3"}

    @pytest.mark.asyncio
    async def test_list_by_resource(self) -> None:
        storage = InMemoryReceiptStorage()
        await storage.store("1", {"id": "1", "resourceUrl": "https://a.com"})
        await storage.store("2", {"id": "2", "resourceUrl": "https://b.com"})
        await storage.store("3", {"id": "3", "resourceUrl": "https://a.com"})

        receipts = await storage.list_by_resource("https://a.com")
        assert len(receipts) == 2

    @pytest.mark.asyncio
    async def test_clear(self) -> None:
        storage = InMemoryReceiptStorage()
        await storage.store("1", {"id": "1", "payer": "0xA"})
        storage.clear()
        result = await storage.retrieve("1")
        assert result is None


# ============================================================================
# Receipt Generator
# ============================================================================


class TestReceiptGenerator:
    @pytest.mark.asyncio
    async def test_generate_basic(self) -> None:
        generator = ReceiptGenerator(format_="eip712")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )
        assert receipt["format"] == "eip712"
        assert receipt["id"] is not None
        assert "signature" in receipt
        assert receipt["payload"]["network"] == "eip155:8453"
        assert receipt["payload"]["amount"] == "10000"

    @pytest.mark.asyncio
    async def test_generate_with_transaction(self) -> None:
        generator = ReceiptGenerator(format_="eip712", include_transaction=True)
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            transaction="0x1234567890abcdef",
        )
        assert receipt["payload"]["transaction"] == "0x1234567890abcdef"

    @pytest.mark.asyncio
    async def test_generate_without_transaction(self) -> None:
        generator = ReceiptGenerator(format_="eip712", include_transaction=False)
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            transaction="0x1234567890abcdef",
        )
        assert receipt["payload"]["transaction"] == ""

    @pytest.mark.asyncio
    async def test_generate_stores_receipt(self) -> None:
        storage = InMemoryReceiptStorage()
        generator = ReceiptGenerator(format_="eip712", storage=storage)
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )
        stored = await storage.retrieve(receipt["id"])
        assert stored is not None
        assert stored["id"] == receipt["id"]

    @pytest.mark.asyncio
    async def test_generate_jws_format(self) -> None:
        generator = ReceiptGenerator(format_="jws")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )
        assert receipt["format"] == "jws"
        # JWS format has 3 dot-separated parts
        assert len(receipt["signature"].split(".")) == 3

    @pytest.mark.asyncio
    async def test_generate_with_custom_sign_fn(self) -> None:
        async def custom_sign(data: bytes) -> str:
            return "0x" + hashlib.md5(data).hexdigest()

        generator = ReceiptGenerator(
            format_="eip712",
            sign_fn=custom_sign,
        )
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )
        assert receipt["signature"].startswith("0x")

    @pytest.mark.asyncio
    async def test_generate_from_settle_context(self) -> None:
        generator = ReceiptGenerator(format_="eip712")

        # Mock settle context
        settle_context = MagicMock()
        settle_context.settle_response.success = True
        settle_context.settle_response.transaction = "0xdeadbeef"

        requirements = MagicMock()
        requirements.network = "eip155:8453"
        requirements.resource = "https://api.example.com/data"
        requirements.amount = "10000"
        requirements.asset = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        requirements.payTo = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"

        settle_context.payload = MagicMock()
        settle_context.payload.from_ = "0x857b06519E91e3A54538791bDbb0E22373e36b66"
        # Use raw dict for payer extraction
        settle_context.payload.raw = {"from": "0x857b06519E91e3A54538791bDbb0E22373e36b66"}

        receipt = await generator.generate_from_settle_context(
            settle_context, requirements
        )
        assert receipt is not None
        assert receipt["payload"]["network"] == "eip155:8453"
        assert receipt["payload"]["transaction"] == "0xdeadbeef"

    @pytest.mark.asyncio
    async def test_generate_from_settle_context_failed(self) -> None:
        generator = ReceiptGenerator(format_="eip712")

        settle_context = MagicMock()
        settle_context.settle_response.success = False

        receipt = await generator.generate_from_settle_context(settle_context, MagicMock())
        assert receipt is None

    @pytest.mark.asyncio
    async def test_generate_from_settle_context_no_payer(self) -> None:
        generator = ReceiptGenerator(format_="eip712")

        settle_context = MagicMock()
        settle_context.settle_response.success = True
        settle_context.settle_response.transaction = "0xdeadbeef"
        settle_context.payload = MagicMock()
        settle_context.payload.raw = {}  # No payer

        requirements = MagicMock()
        requirements.network = "eip155:8453"
        requirements.resource = "https://api.example.com/data"
        requirements.amount = "10000"
        requirements.asset = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        requirements.payTo = "0x209693Bc6afc0C5328bA36FaF03C514EF312287C"

        receipt = await generator.generate_from_settle_context(
            settle_context, requirements
        )
        assert receipt is None


# ============================================================================
# Receipt Verifier
# ============================================================================


class TestReceiptVerifier:
    @pytest.mark.asyncio
    async def test_verify_valid_eip712(self) -> None:
        generator = ReceiptGenerator(format_="eip712")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )

        verifier = ReceiptVerifier()
        result = await verifier.verify(receipt)
        assert result.valid is True
        assert result.receipt_data["network"] == "eip155:8453"

    @pytest.mark.asyncio
    async def test_verify_valid_jws(self) -> None:
        generator = ReceiptGenerator(format_="jws")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )

        verifier = ReceiptVerifier()
        result = await verifier.verify(receipt)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_verify_unsupported_format(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify({"format": "unknown", "signature": "x"})
        assert result.valid is False
        assert "Unsupported format" in result.error

    @pytest.mark.asyncio
    async def test_verify_missing_signature(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify({"format": "eip712", "payload": {}})
        assert result.valid is False
        assert "Missing signature" in result.error

    @pytest.mark.asyncio
    async def test_verify_missing_payload(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify({"format": "eip712", "signature": "0x123"})
        assert result.valid is False
        assert "Missing payload" in result.error

    @pytest.mark.asyncio
    async def test_verify_invalid_payload_fields(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify(
            {
                "format": "eip712",
                "payload": {"version": 1, "network": "eip155:8453"},
                "signature": "0x123",
            }
        )
        assert result.valid is False
        assert "Missing required payload fields" in result.error

    @pytest.mark.asyncio
    async def test_verify_wrong_version(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify(
            {
                "format": "eip712",
                "payload": {
                    "version": 99,
                    "network": "eip155:8453",
                    "resourceUrl": "https://a.com",
                    "amount": "100",
                    "asset": "0x123",
                    "payTo": "0xA",
                    "payer": "0xB",
                    "issuedAt": 123,
                },
                "signature": "0x123",
            }
        )
        assert result.valid is False
        assert "Unsupported receipt version" in result.error

    @pytest.mark.asyncio
    async def test_verify_with_expected_signer(self) -> None:
        generator = ReceiptGenerator(format_="eip712")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )

        # Get the actual signer from the receipt
        verifier_no_check = ReceiptVerifier()
        result_no_check = await verifier_no_check.verify(receipt)
        actual_signer = result_no_check.signer

        # Verify with correct expected signer
        verifier_correct = ReceiptVerifier(expected_signer=actual_signer)
        result_correct = await verifier_correct.verify(receipt)
        assert result_correct.valid is True

        # Verify with wrong expected signer
        verifier_wrong = ReceiptVerifier(expected_signer="0xwrongaddress")
        result_wrong = await verifier_wrong.verify(receipt)
        assert result_wrong.valid is False
        assert "Signer mismatch" in result_wrong.error

    @pytest.mark.asyncio
    async def test_verify_from_extension(self) -> None:
        generator = ReceiptGenerator(format_="eip712")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )

        extension_data = {
            "info": {"receipt": receipt},
        }

        verifier = ReceiptVerifier()
        result = await verifier.verify_from_extension(extension_data)
        assert result.valid is True

    @pytest.mark.asyncio
    async def test_verify_from_extension_no_receipt(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify_from_extension({"info": {}})
        assert result.valid is False
        assert "No receipt found" in result.error

    @pytest.mark.asyncio
    async def test_verify_invalid_jws_format(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify({"format": "jws", "signature": "not-a-jws"})
        assert result.valid is False
        assert "Invalid JWS format" in result.error

    @pytest.mark.asyncio
    async def test_verify_invalid_jws_header(self) -> None:
        verifier = ReceiptVerifier()
        result = await verifier.verify(
            {
                "format": "jws",
                "signature": "!!!invalid!!!.payload.sig",
            }
        )
        assert result.valid is False


# ============================================================================
# Extension Helpers
# ============================================================================


class TestExtensionHelpers:
    def test_extract_receipt_from_extensions(self) -> None:
        receipt = {"format": "eip712", "signature": "0x123"}
        extensions = {RECEIPT_ATTESTATION: {"info": {"receipt": receipt}}}
        result = extract_receipt_from_extensions(extensions)
        assert result == receipt

    def test_extract_receipt_from_extensions_missing(self) -> None:
        result = extract_receipt_from_extensions({})
        assert result is None

    def test_is_receipt_attestation_extension(self) -> None:
        assert is_receipt_attestation_extension({"info": {"enabled": True}}) is True
        assert is_receipt_attestation_extension({"info": {}}) is False
        assert is_receipt_attestation_extension({}) is False
        assert is_receipt_attestation_extension("not a dict") is False


# ============================================================================
# Resource Server Extension
# ============================================================================


class TestReceiptAttestationResourceServerExtension:
    def test_key(self) -> None:
        generator = ReceiptGenerator()
        ext = ReceiptAttestationResourceServerExtension(generator)
        assert ext.key == RECEIPT_ATTESTATION

    def test_enrich_declaration(self) -> None:
        generator = ReceiptGenerator(format_="eip712")
        ext = ReceiptAttestationResourceServerExtension(generator)
        result = ext.enrich_declaration({}, None)
        assert result["info"]["enabled"] is True
        assert result["info"]["supportedFormats"] == ["eip712"]
        assert "schema" in result

    def test_enrich_declaration_merges(self) -> None:
        generator = ReceiptGenerator()
        ext = ReceiptAttestationResourceServerExtension(generator)
        existing = {"info": {"existing": True}}
        result = ext.enrich_declaration(existing, None)
        assert result["info"]["existing"] is True
        assert result["info"]["enabled"] is True

    def test_enrich_settlement_response(self) -> None:
        generator = ReceiptGenerator()
        ext = ReceiptAttestationResourceServerExtension(generator)
        result = ext.enrich_settlement_response({}, MagicMock())
        assert result is None


class TestReceiptAttestationServerExtensionFactory:
    def test_creates_extension(self) -> None:
        generator = ReceiptGenerator()
        ext = receipt_attestation_server_extension(generator)
        assert isinstance(ext, ReceiptAttestationResourceServerExtension)
        assert ext.key == RECEIPT_ATTESTATION


# ============================================================================
# JSON Schemas
# ============================================================================


class TestSchemas:
    def test_receipt_attestation_schema(self) -> None:
        assert receipt_attestation_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert receipt_attestation_schema["type"] == "object"
        assert "enabled" in receipt_attestation_schema["properties"]
        assert "supported_formats" in receipt_attestation_schema["properties"]

    def test_receipt_payload_schema(self) -> None:
        assert receipt_payload_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert receipt_payload_schema["type"] == "object"
        required = receipt_payload_schema["required"]
        assert "version" in required
        assert "network" in required
        assert "resourceUrl" in required
        assert "amount" in required
        assert "payTo" in required
        assert "payer" in required
        assert "issuedAt" in required


# ============================================================================
# ReceiptInfo Model
# ============================================================================


class TestReceiptInfoModel:
    def test_default(self) -> None:
        info = ReceiptInfo()
        assert info.enabled is True
        assert info.supported_formats == ["eip712"]

    def test_disabled(self) -> None:
        info = ReceiptInfo(enabled=False)
        assert info.enabled is False


# ============================================================================
# Integration: Generate then Verify roundtrip
# ============================================================================


class TestRoundtrip:
    @pytest.mark.asyncio
    async def test_eip712_roundtrip(self) -> None:
        storage = InMemoryReceiptStorage()
        generator = ReceiptGenerator(
            format_="eip712",
            storage=storage,
            include_transaction=True,
        )
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
            transaction="0xdeadbeef",
        )

        # Retrieve from storage
        stored = await storage.retrieve(receipt["id"])
        assert stored is not None

        # Verify
        verifier = ReceiptVerifier()
        result = await verifier.verify(stored)
        assert result.valid is True
        assert result.receipt_data["network"] == "eip155:8453"
        assert result.receipt_data["payer"] == "0x857b06519E91e3A54538791bDbb0E22373e36b66"

    @pytest.mark.asyncio
    async def test_jws_roundtrip(self) -> None:
        generator = ReceiptGenerator(format_="jws")
        receipt = await generator.generate(
            network="eip155:8453",
            resource_url="https://api.example.com/data",
            amount="10000",
            asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            pay_to="0x209693Bc6afc0C5328bA36FaF03C514EF312287C",
            payer="0x857b06519E91e3A54538791bDbb0E22373e36b66",
        )

        verifier = ReceiptVerifier()
        result = await verifier.verify(receipt)
        assert result.valid is True
        # JWS signer comes from kid header
        assert result.signer != ""
