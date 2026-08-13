# Agent Alerts

Standalone urgent-attention bridge for local agents.

The goal is simple:

1. An agent creates an urgent request.
2. A WhatsApp notification is sent when a provider is configured.
3. The agent waits on a shared response file.
4. A separate process captures the operator reply and writes that response file.

State lives under `/home/arthe/agent-alerts`:

- `requests/` holds pending request JSON files.
- `responses/` holds captured operator replies.
- `outbox/` holds file-backed notification bodies and provider send metadata.
- `logs/` holds webhook receiver logs.

## Agent Usage

Create a request and wait for a response:

```bash
agent-alert ask \
  --agent Harbor \
  --title "Upgrade needs approval" \
  --wait \
  --timeout 3600 \
  "Continue with the risky step?"
```

Write a manual response if WhatsApp is not wired yet:

```bash
agent-alert respond 20260424T220000Z-abc123ef "yes, continue"
```

List unresolved requests:

```bash
agent-alert pending
```

Show the full request and response:

```bash
agent-alert show 20260424T220000Z-abc123ef
```

## WhatsApp Wiring With Twilio

This tool supports Twilio's WhatsApp Programmable Messaging API and incoming message webhooks using only the Python standard library.

Set credentials outside the repo, for example in a private shell profile or systemd environment file:

```bash
export AGENT_ALERT_PROVIDER=twilio
export TWILIO_ACCOUNT_SID=ACxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
export TWILIO_AUTH_TOKEN='<twilio-auth-token>'
export TWILIO_WHATSAPP_FROM='whatsapp:+14155238886'
export AGENT_WHATSAPP_TO='whatsapp:+5215555555555'
```

Run the receiver:

```bash
agent-alert webhook-server --host 127.0.0.1 --port 8787
```

Expose `/twilio` through the existing HTTPS ingress or a temporary tunnel, then configure the Twilio WhatsApp sender incoming-message webhook to `POST` there.

When the operator replies, either include the request ID from the notification or reply while only one request is pending. The webhook writes:

```text
/home/arthe/agent-alerts/responses/<request-id>.json
```

The waiting agent sees that file and resumes.

## Provider Notes

Without Twilio environment variables, `agent-alert ask` falls back to the file outbox. That keeps the request/wait/response path testable without credentials.

WhatsApp business-initiated notifications can require pre-approved templates when there is no active user-initiated conversation window. If Twilio rejects a free-form message, the send error is recorded in `outbox/<request-id>.send.json`; the shared response workflow still works after configuring an approved notification template or opening a conversation window.
