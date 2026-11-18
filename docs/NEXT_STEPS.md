# Next steps (smallest executable task first)

1. **Razorpay test keys** (owner): create a Razorpay account in test mode, save keys with
   `scripts/set_secret.ps1`, add the webhook (`<tunnel>/v1/webhooks/razorpay`), then run a real test
   payment end to end in Telegram. See `integrations/SETUP.md`.
2. **Apply for partner access** (owner): Swiggy Builders Club (Instamart + Food) is the most direct
   route to real grocery/food orders in chat; then Zomato MCP, Uber Ride Requests, Booking.com Demand
   API. Each approval → adapter written against the real docs → registry status updated → recorded-fixture
   tests.
3. **Planner eval**: 50 Hinglish / Hindi / Marathi / English requests across groceries, rides, hotels,
   weather and movies, scored on the chosen tool and arguments. Run on Gemini flash-lite before changing
   the prompt again.
4. **Address consent**: "save this address?" → stored per user, encrypted; reuse with confirmation.
5. **Hindi/Marathi strings** for the commerce replies.
6. **Stable public URL** (named Cloudflare tunnel or ingress) so Telegram/Razorpay webhooks survive
   restarts; and drop the OTP path from access logs.
7. **PhonePe PG adapter** (UAT sandbox) as a second gateway; Tripadvisor reviews adapter.
8. **Last-resort reply on workflow failure** (OPEN_ISSUES #9) and a ~20 s planner timeout (#12).
9. Memory with consent; Gmail OAuth; webhook rate limiting; refuse the default JWT secret outside local;
   Azure IaC (no provisioning without approval).
10. **Rotate the secrets that were pasted in chat**: OpenAI key, Meta App secret, WhatsApp token, Gemini
    key, Telegram bot token (`/revoke` in @BotFather).
