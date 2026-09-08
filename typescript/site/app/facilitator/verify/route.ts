import { VerifyResponse } from "@x402/core/types";
import { parsePaymentPayload, parsePaymentRequirements } from "@x402/core/schemas";
import { getFacilitator } from "../index";

/**
 * Handles POST requests to verify x402 payments
 *
 * @param req - The incoming request containing payment verification details
 * @returns A JSON response indicating whether the payment is valid
 */
export async function POST(req: Request) {
  // Parse request body - handle JSON parsing errors separately
  let body: Record<string, unknown>;

  try {
    body = await req.json();
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    console.error("Failed to parse request body:", errorMessage);
    return Response.json(
      {
        isValid: false,
        invalidReason: "invalid_json",
        invalidMessage: "Failed to parse request body",
        error: "Failed to parse request body",
      } as VerifyResponse,
      { status: 400 },
    );
  }

  // Check for missing parameters
  if (!body.paymentPayload || !body.paymentRequirements) {
    return Response.json(
      {
        isValid: false,
        invalidReason: "missing_parameters",
        invalidMessage: "Missing paymentPayload or paymentRequirements",
        error: "Missing paymentPayload or paymentRequirements",
      } as VerifyResponse,
      { status: 400 },
    );
  }

  // Validate schemas before forwarding to the facilitator.
  // Without this, a malformed v2 payload (e.g. missing `accepted`) would
  // produce an opaque 500 deep inside the scheme handler instead of a
  // clear 400 the caller can fix.
  const payloadResult = parsePaymentPayload(body.paymentPayload);
  if (!payloadResult.success) {
    return Response.json(
      {
        isValid: false,
        invalidReason: "invalid_payment_payload",
        invalidMessage: `paymentPayload validation failed: ${payloadResult.error.issues.map(i => i.message).join(", ")}`,
        error: payloadResult.error.message,
      } as VerifyResponse,
      { status: 400 },
    );
  }

  const requirementsResult = parsePaymentRequirements(body.paymentRequirements);
  if (!requirementsResult.success) {
    return Response.json(
      {
        isValid: false,
        invalidReason: "invalid_payment_requirements",
        invalidMessage: `paymentRequirements validation failed: ${requirementsResult.error.issues.map(i => i.message).join(", ")}`,
        error: requirementsResult.error.message,
      } as VerifyResponse,
      { status: 400 },
    );
  }

  try {
    const facilitator = await getFacilitator();

    // Hooks will automatically:
    // - Track verified payment (onAfterVerify)
    // - Extract and catalog discovery info (onAfterVerify)
    const response: VerifyResponse = await facilitator.verify(
      payloadResult.data,
      requirementsResult.data,
    );

    return Response.json(response);
  } catch (error) {
    const errorMessage = error instanceof Error ? error.message : String(error);
    console.error("Verify error:", errorMessage);
    return Response.json(
      {
        isValid: false,
        invalidReason: "unexpected_error",
        invalidMessage: errorMessage,
        error: errorMessage,
      } as VerifyResponse,
      { status: 500 },
    );
  }
}

/**
 * Provides API documentation for the verify endpoint
 *
 * @returns A JSON response describing the verify endpoint and its expected request body
 */
export async function GET() {
  return Response.json({
    endpoint: "/verify",
    description: "POST to verify x402 payments",
    body: {
      paymentPayload: "PaymentPayload",
      paymentRequirements: "PaymentRequirements",
    },
  });
}
