# WhatsApp compliance

## The restriction

Meta changed the WhatsApp Business Solution terms in October 2025 (in force **15 January 2026**).
"AI providers" may no longer distribute **general-purpose AI assistants** through the WhatsApp Business
API. Businesses that use AI *as part of* a customer-service workflow are still allowed. ChatGPT, Copilot
and Perplexity left WhatsApp because of this change.

Apna AI is a general-purpose personal assistant, so a public launch on WhatsApp is **not permitted**
unless Meta explicitly approves this exact product. Reports of regulators challenging the policy (Italy,
Brazil, EU) are unconfirmed here and change nothing until Meta changes its terms.

## What the code does

| Rule | Where | Test |
|---|---|---|
| Official Cloud API (Graph `v26.0`) only; no WhatsApp Web automation, unofficial libraries, QR sessions or consumer numbers | `apna/channels/whatsapp.py` | — |
| Production delivery off by default (`WHATSAPP_PRODUCTION_ENABLED=false`); only `WHATSAPP_ALLOWED_TEST_NUMBERS` receive messages, everything else is `BLOCKED_POLICY` | `WhatsAppAdapter._policy_allows` | `test_production_gate_blocks_non_test_numbers`, `test_whatsapp_non_test_number_is_blocked` |
| Inbound webhooks need a valid `X-Hub-Signature-256` HMAC (app secret) | `verify_signature`, `api/webhooks.py` | `test_signature_verification`, `test_whatsapp_rejects_bad_signature` |
| Verify-token handshake | `GET /v1/webhooks/whatsapp` | `test_whatsapp_verify_handshake` |
| Free-form messages only inside the 24 h customer-service window; outside it only an approved template, otherwise nothing is sent | `build_payload` | `test_payload_window_and_buttons` |
| Duplicate webhook deliveries are ignored (dedupe on `wamid`) | `Message.dedupe_key` | `test_whatsapp_end_to_end_with_button_approval` |
| Approvals use interactive reply buttons bound to an exact-action approval | `approval_buttons` | same |
| Delivery status webhooks (`sent/delivered/read/failed`) update `outbound_messages` | `ConversationService.handle_inbound` | — (unverified against Meta) |
| The assistant says it is an AI assistant | Reply templates | — (onboarding message still to be written) |

## Status

**BLOCKED_EXTERNAL_ACCESS** for production. Two things are missing: Meta permission for this product,
and a Meta app with the WhatsApp product added. The adapter is **WORKING_IN_MOCK**: every request shape
is tested against a recording transport, and nothing has been sent to Meta yet.

## To test with Meta's free test number (no policy issue for development)

1. Create a Meta developer app, add the WhatsApp product, and note the test **phone number ID** and a
   **temporary access token**.
2. Add your own phone as a recipient on the test number, then put it (digits only, with country code)
   in `WHATSAPP_ALLOWED_TEST_NUMBERS`.
3. Expose `POST/GET /v1/webhooks/whatsapp` over HTTPS (for example a tunnel), and set the callback URL
   and `WHATSAPP_VERIFY_TOKEN` in the app. Subscribe to `messages`.
4. Set `WHATSAPP_ENABLED=true`, `WHATSAPP_APP_SECRET` and `WHATSAPP_ACCESS_TOKEN`.

## Launch path

Launch on **Telegram** (bots and AI assistants are permitted). Keep WhatsApp in test mode. Turn on
`WHATSAPP_PRODUCTION_ENABLED` only with written confirmation from Meta, or if the terms change.
