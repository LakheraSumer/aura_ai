# Run Apna AI 24x7 on a free server (no website)

Apna runs as four always-on containers plus a database: `postgres`, `redis`, `temporal`, `worker` and
`telegram`. The `telegram` container **polls** Telegram for new messages, so the server needs no website,
public URL, domain or open ports. Everything restarts on its own after a crash or reboot.

## Which free plan

| Platform | Free for how long | Fits Apna? |
|---|---|---|
| **Oracle Cloud "Always Free"** (Ampere A1 ARM VM) | No end date | **Yes.** 2 CPUs + 12 GB RAM since June 2026 (was 4 + 24); Apna needs about 3–4 GB |
| Google Cloud free tier (e2-micro) | No end date | Too small: 1 GB RAM, US regions only |
| AWS / Azure | Credits for a limited time (months), then paid | Not free long-term |
| Render / Koyeb / similar free tiers | Sleep when idle or no background workers | No: reminders and alerts need an always-on worker |

Oracle notes: it asks for a card to verify you (no charge within Always Free). Popular regions (Mumbai,
Hyderabad) are sometimes "out of capacity" for A1; retry later or choose another region. Oracle may
reclaim *idle* Always Free VMs; upgrading the account to Pay-As-You-Go stops that and still costs ₹0 while
you stay within the Always Free limits. Check the live limits:
https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm

## 1. Create the server (you, ~15 minutes)

1. Sign up at https://www.oracle.com/cloud/free/ and pick a home region near you.
2. Compute → Instances → **Create instance**:
   - Image: **Canonical Ubuntu 24.04**
   - Shape: **Ampere → VM.Standard.A1.Flex**, 2 OCPU, 12 GB memory
   - Add your SSH public key (or let Oracle generate one and download the private key).
3. Leave inbound ports as they are (SSH only). Apna needs nothing else open.
4. Note the instance's **public IP**.

## 2. Copy the project and your keys (from your PC)

From the folder that contains `apna-ai` (the archive skips the local Python environment; `.env` is inside):

```powershell
tar --exclude=.venv --exclude=__pycache__ -czf apna-ai.tgz apna-ai
scp -i <your-key> apna-ai.tgz ubuntu@<public-ip>:~/
ssh -i <your-key> ubuntu@<public-ip> "tar -xzf apna-ai.tgz && rm apna-ai.tgz"
```

Keys travel only over SSH. Good moment to rotate the keys that were pasted in chat before copying.

## 3. Start it (on the server)

```bash
ssh -i <your-key> ubuntu@<public-ip>
cd ~/apna-ai && bash scripts/server_setup.sh
```

The script installs Docker (Ubuntu packages), creates a random database password, adds swap, builds the
image and starts everything. When `telegram` starts it removes the old webhook and begins polling.

## 4. Stop the copy on your PC

Only one copy may poll the same bot. On the PC:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\docker_stack.ps1 down
```

The server starts with an empty database: your chats get fresh accounts on their first message.

## Everyday commands (on the server, in `~/apna-ai/infrastructure/server`)

| What | Command |
|---|---|
| Status | `sudo docker compose ps` |
| Live logs | `sudo docker compose logs -f telegram worker` |
| Update after code changes | copy the new code, then `bash ~/apna-ai/scripts/server_setup.sh` |
| Restart | `sudo docker compose restart` |
| Stop (data kept) | `sudo docker compose down` |
| Backup the database | `sudo docker compose exec postgres pg_dump -U apna apna > backup.sql` |

## Not available in this mode

- WhatsApp, Razorpay webhooks and Google sign-in need a public HTTPS URL. Razorpay payments still complete
  without one (the payment workflow polls Razorpay's status API). Add a Cloudflare tunnel later if needed.
