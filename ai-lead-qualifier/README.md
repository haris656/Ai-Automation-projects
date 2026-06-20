# AI Inbound Lead Router (n8n)

An n8n workflow that turns a public contact form submission into a scored, routed, and logged sales lead, with security and error handling built in from the start.

A lead comes in through a signed webhook. The workflow verifies the signature, validates and sanitizes the input, enriches it, asks Claude to score and tier it, routes hot leads to a Slack alert and a personalized auto-reply, logs every lead to a Google Sheet, and fails safely at every step so a transient API problem never drops a lead.

This is a portfolio project. It is built to import cleanly and read clearly. You add your own credentials to run it.

---

## What it does

1. **Receives** a POST to `/webhook/lead-intake` with `name`, `email`, `company`, `message`.
2. **Verifies** an HMAC-SHA256 signature so only trusted senders can trigger it.
3. **Validates and sanitizes** the input, rejecting bad or unsigned requests with a clean `4xx`.
4. **Enriches** the lead with a light signal (business vs personal email, domain).
5. **Scores** the lead 0 to 100 and assigns a tier (hot / warm / cold) using Claude, with the user text fenced as data to resist prompt injection.
6. **Guards** the model output: defensive parse, shape validation, score clamping, and a manual-review fallback if anything is off.
7. **Routes**: hot leads get a Slack alert plus a personalized email auto-reply; warm and cold are logged quietly.
8. **Logs** every lead to a Google Sheets audit trail (no secrets logged).
9. **Fails safely**: each external call has retries with backoff and an error output that alerts instead of dropping the lead.

---

## Architecture

```
Webhook (signed)
   -> Verify & Validate (HMAC + validation + sanitization)
       -> Gate
           |-- invalid --> Respond 4xx
           +-- valid   --> Build Enrichment
                            -> Claude Score (HTTP, retries)
                                |-- ok    --> Parse & Guard LLM
                                +-- error --> Scoring Error Handler (manual review)
                                                 -> Tier Router
                                                     |-- hot  --> Slack Alert -> Email Auto-Reply
                                                     |-- warm --> (log)
                                                     +-- cold --> (log)
                                                                   -> Merge
                                                                      -> Build Log Row
                                                                         -> Append to Google Sheets
                                                                            -> Respond 200

(Slack / Email / Sheets error outputs) -> Failure Collector -> Slack Failure Alert
```

The workflow canvas includes sticky notes that explain each section.

---

## Requirements

- **n8n** version **1.x** (built and validated on a recent 1.x instance).
- Accounts and keys for: **Anthropic** (Claude), **Slack**, **Google Sheets**, and an **SMTP** mailbox.

### Node versions used

The workflow pins these node versions. They are current as of build time on a recent n8n 1.x release.

| Node | Type | Version |
|------|------|---------|
| Webhook | `n8n-nodes-base.webhook` | 2.1 |
| Code | `n8n-nodes-base.code` | 2 |
| Switch | `n8n-nodes-base.switch` | 3.4 |
| HTTP Request | `n8n-nodes-base.httpRequest` | 4.4 |
| Respond to Webhook | `n8n-nodes-base.respondToWebhook` | 1.5 |
| Merge | `n8n-nodes-base.merge` | 3.2 |
| Slack | `n8n-nodes-base.slack` | 2.4 |
| Email Send | `n8n-nodes-base.emailSend` | 2.1 |
| Google Sheets | `n8n-nodes-base.googleSheets` | 4.7 |

---

## Setup

### 1. Import the workflow

In n8n: **Workflows -> Import from File** and select `workflows/ai-inbound-lead-router.json`.

The workflow imports **inactive** and with **no credentials attached**. That is intentional. You attach your own.

### 2. Configuration overview: two kinds of secrets

This project deliberately separates secrets the way n8n actually handles them:

- **Environment variables** (read by Code nodes via `$env`) live in your n8n host environment. There are only two.
- **API credentials** (Anthropic, Slack, Google, SMTP) are **not** environment variables. They live in n8n's encrypted **Credentials** store and are selected on the relevant nodes.

Getting this distinction right is the whole point: n8n does not read API keys from a `.env` file the way a typical web app does.

### 3. Set the environment variables

Copy the example file and fill in your values:

```bash
cp .env.n8n.example .env.n8n
```

```bash
# .env.n8n
N8N_LEAD_HMAC_SECRET=replace_with_a_long_random_secret   # openssl rand -hex 32
N8N_LEAD_FROM_EMAIL=you@yourdomain.com
```

Make these available to the n8n process (host env, container env, or n8n's own env file), then restart n8n.

| Variable | Used by | Purpose |
|----------|---------|---------|
| `N8N_LEAD_HMAC_SECRET` | Verify & Validate node | Secret for verifying the incoming webhook signature |
| `N8N_LEAD_FROM_EMAIL` | Email Hot Auto-Reply node | The "from" address on the auto-reply |

### 4. Create the credentials (in n8n, not in any file)

Create these in **n8n -> Credentials** and select them on the matching nodes:

| Credential | n8n type | Used by | Notes |
|------------|----------|---------|-------|
| Anthropic key | **Header Auth** | Claude Score | Header name `x-api-key`, value = your Anthropic key |
| Slack | **Slack API** | Slack Hot Alert, Slack Failure Alert | OAuth2 or bot token with `chat:write` |
| Google Sheets | **Google Sheets / Google API** | Append Audit Log | Access to your target sheet |
| SMTP | **SMTP** | Email Hot Auto-Reply | Host, port, user, password |

> The Claude call uses the HTTP Request node with a generic **Header Auth** credential rather than the LangChain Anthropic node. This keeps the auth boundary explicit, avoids LangChain-node version drift, and makes the prompt structure easy to read and audit.

### 5. Point the placeholder fields at your resources

- **Slack Hot Alert** and **Slack Failure Alert**: set the channel (placeholder `C0000000000`).
- **Append Audit Log**: set the Google Sheet (placeholder `YOUR_GOOGLE_SHEET_ID`) and the sheet/tab name (`Leads`). Create a sheet with a header row matching the logged fields: `timestamp, name, email, company, email_type, domain, score, tier, reason, parsed_ok, api_failed, message`.

### 6. Activate and test

Activate the workflow, then send a signed test request:

```bash
N8N_LEAD_HMAC_SECRET=your_secret \
WEBHOOK_URL=https://your-n8n-host/webhook/lead-intake \
node sign-request.js
```

A valid request returns `{ "ok": true, "tier": "...", "score": ... }`. An unsigned or invalid request returns a `4xx` with a reason.

---

## Sending leads from your site

Sign the raw JSON body with the same secret and send the hex digest in `X-Signature`:

```js
const crypto = require('crypto');
const body = JSON.stringify({ name, email, company, message });
const signature = crypto.createHmac('sha256', process.env.HMAC_SECRET).update(body).digest('hex');
// POST `body` with headers: { 'Content-Type': 'application/json', 'X-Signature': signature }
```

The body must be sent byte for byte identical to what was signed.

---

## Version compatibility notes

n8n evolves quickly, so a few things are worth knowing before importing into a different instance:

- **Node typeVersions.** This workflow pins specific node versions (table above). On an **older** n8n, a pinned version may not exist yet; on a **much newer** n8n, a node may have a higher default version with slightly different parameters. If a node imports with a warning, open it, let n8n migrate it to the available version, and re-check its fields. The logic does not change; only the node shell might.

- **Switch and If nodes.** The Switch node (v3.4) uses the v2 filter/condition format. Older n8n versions used a different condition structure. If you are on an older release, the Gate and Tier Router nodes may need their conditions re-selected in the UI.

- **HTTP Request node (v4.4).** The Claude call relies on the v4.x body/header structure. On very old n8n, the HTTP node had a different parameter layout; re-select the body type as **JSON** if needed.

- **Webhook response mode.** The webhook uses `responseMode: responseNode` with `Respond to Webhook` nodes. This requires the webhook's `onError` to allow a response on error, which is already set. Keep it if you edit the node.

- **Anthropic model + API version.** The call targets `claude-sonnet-4-6` with `anthropic-version: 2023-06-01`. If Anthropic changes model names or the messages API, update the model string in the **Claude Score** node body and the version header.

- **Credentials are not included.** By design, nothing runs until you attach your own credentials and set the env vars.

If you import into a fresh n8n and hit a node warning, it is almost always a version migration, not a logic error. Open the flagged node, accept the migration, confirm the fields, and run again.

---

## Project structure

```
ai-lead-qualifier/
  README.md                                  This file
  SECURITY.md                                Security design notes
  .env.n8n.example                           Placeholder environment variables (the two real $env vars)
  .gitignore                                 Keeps real secrets out of git
  sign-request.js                            Helper to send a signed test request
  workflows/
    ai-inbound-lead-router.json              The workflow (import this)
```

---

## Notes

This is a demonstration project. It shows a complete, security-conscious n8n pipeline: signed webhook intake, validation and sanitization, LLM scoring with output guards, tiered routing, audit logging, and safe failure handling. Adapt the prompt, scoring thresholds, and routing to your own use case.
