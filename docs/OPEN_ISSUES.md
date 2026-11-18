# Open issues

1. **Two unexplained single integration failures**, each seen once and not reproduced:
   `test_14_worker_restart…` early in checkpoint 1, and `test_04_train_search…` in the final run. Only
   the summary line was captured, not the assertion. Across the session: 9 clean full-suite runs, 3 clean
   integration-only runs and 6 targeted loops. Treat these as real flakes until explained. Next time,
   keep the full log (`pytest tests -q > run.log`), and consider dumping the Temporal workflow history in
   `wait_task` when it times out.
2. **Projection lag**: the step status, task status and reply message are written by consecutive
   activities, so for a few hundred ms a poller can see a step in `AWAITING_*` while the task is still
   `RUNNING`. This is harmless, but API clients must not treat it as final. Option: write task status
   and reply in one activity.
3. **Repeated approval card**: when a clarification arrives while an approval is still pending, the old
   task re-publishes its whole reply (draft and buttons). It should publish only what changed.
4. **Docker image**: the base target builds and runs the api, worker and webhook instance (`scripts/docker_stack.ps1`). Not built yet: the browser
   target (`--with-deps chromium`), and a docker-compose service set that runs the API and workers as
   containers.
5. **Windows path length**: this checkout's path is longer than 260 characters, so Playwright's driver
   and SQLAlchemy's compiled modules fail to load unless commands run through the 8.3 short path. Move
   the repo to a short path (e.g. `D:\Project sumer\projects\apna-ai`) to remove the workaround.
6. The rule planner's "message" intent always maps to email. With Claude configured, the planner can
   distinguish email from chat messages, but no channel can yet send to third parties.
7. **Windows Smart App Control blocks native Python modules on this PC** (since 2026-11-14; the full suite now runs in a container, see Checkpoint 3:
   `temporal_sdk_bridge`, `jiter`, SQLAlchemy `_result_cy`). That's a Windows security setting and
   was left alone. Workaround: everything runs in Docker (`scripts/docker_stack.ps1 up -Dev`). To do:
   run the integration suite in a container too.
8. **Docker Desktop lost container networking once**: infra containers showed "Up" with no IP. Fixed
   by `docker compose restart`. Watch for it after sleep/resume.
9. **A workflow that fails outside an activity's own error handling sends the user nothing** (the two
   "HELLO/HI" tasks died after 5 planner retries). The planner fallback removes the common cause, but
   a last-resort "Sorry, something went wrong" reply on workflow failure is still missing.
10. **Quick tunnel URL changes on every restart**, so the Meta callback must be re-registered. Use a
    named Cloudflare tunnel or real ingress before relying on it.
11. **WhatsApp test number sends only to numbers confirmed in Meta's API Setup** (error 131030). Our
    own allow-list (`WHATSAPP_ALLOWED_TEST_NUMBERS`) is a second, separate gate.
12. Gemini free-tier latency varies (1.5 s typical, ~30 s spikes) and the newest flash models time out.
    Consider a per-call timeout of ~20 s before falling back.
13. **Commerce replies are English/Hinglish only.** Hindi and Marathi users get English text for grocery,
    payment, weather and handoff messages for now.
14. **Delivery addresses aren't remembered** across conversations (no consent flow for saving one yet).
    The address is kept only on the order and in the chat history.
15. **Sandbox data is static:** prices, stock, serviceability, ETAs and "on the way" tracking are
    simulated. Ties (e.g. Blinkit = Zepto ₹215) go to the first store in the list.
16. **Razorpay is verified only against a mocked API.** The first real test-mode run needs the owner's
    test keys and a webhook pointed at the tunnel URL (which changes on restart).
17. **Adapters not built:** PhonePe PG and Tripadvisor (both registered as credentials-required).
    Partner-only providers need access first.
18. **The OTP page token appears in the access log path** of the webhook-only instance. It's single-use,
    expires in 5 minutes and is useless after submission, but access logs should drop that path before
    production.
19. **The free-tier planner sometimes picks the wrong tool** for unusual phrasing (a Hinglish hotel
    request first got the generic help reply; fixed with prompt examples). The planner eval in
    NEXT_STEPS is still missing.
20. **Offers prices are TEST data.** Real prices need Amazon PA-API / Flipkart affiliate / travel partner
    access (see SETUP). Ride fares are estimates only and are never booked in chat.
21. **Offer-booking Razorpay path** reuses the grocery PaymentWorkflow but has no dedicated integration
    test yet (the sandbox path is tested).
22. **Google needs a fixed public URL** (quick tunnels change every restart) and Google's verification of
    Gmail scopes before anyone but listed test users can connect.
23. **Price alerts check every 6 hours** (`PRICE_WATCH_POLL_SECONDS`); sandbox prices change once a day.
24. **Reminder buttons are not deduplicated** per tap: a double-tap on Snooze snoozes twice (capped at 3).
