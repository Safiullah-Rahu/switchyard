# Integrations: idempotency and verification, API by API

Every adapter answers three questions: how the key travels with the write (**execute**), how an earlier
write is found after a crash or timeout (**lookup**), and how the write is proven (**verify**). The table
gives the method planned for each common system.

**Status key:**
- **built**: implemented and tested in this repository.
- **plan**: the method that would be built.
- **confirm**: behaviour to check against a test account during discovery, before relying on it.

| System | Execute (key travels as…) | Lookup | Verify | Status |
|---|---|---|---|---|
| HubSpot contacts | batch upsert with `idProperty=email` | not needed (upsert) | GET by id | built (mocked HTTP) |
| HubSpot deals / notes | custom property `switchyard_key` with *unique value* on; a retry gets a conflict | search `switchyard_key EQ key` | GET by id (search lags writes) | built (mocked HTTP) |
| Salesforce | External ID field + upsert `PATCH /sobjects/Opportunity/Switchyard_Key__c/{key}` | the upsert *is* the lookup | GET by id | plan |
| Pipedrive / other CRMs | custom field holding the key + search before create | search by field | GET by id | plan + confirm |
| Gmail (send) | `Message-ID: <key@domain>` plus an `X-Switchyard-Key` header in the raw RFC 2822 message | `users.messages.list q=rfc822msgid:` | `users.messages.get`, compare subject, recipients, body hash | plan + confirm that Message-ID is preserved |
| Microsoft 365 mail (Graph) | create a draft carrying a single-value extended property set to the key, then send the draft | `$filter` on the extended property in Sent Items | GET message, compare | plan + confirm |
| Google Calendar | client-supplied event `id` (base32hex of the key); a retry gets 409 | `events.get(id)` | compare start, end, title | built (simulated) |
| Outlook calendar (Graph) | event `transactionId` = key (Graph's own retry-dedup field) | `$filter` on `transactionId` | GET event | plan |
| Google Drive | `appProperties.switchyard_key` on `files.create` | `files.list q="appProperties has {key='switchyard_key' and value='…'}"` | `files.get`, compare name and content hash | built (simulated) |
| SharePoint / OneDrive | deterministic file path from the key, with `@microsoft.graph.conflictBehavior=fail` | GET by path | GET item, compare | plan |
| Slack | message `metadata.event_payload.idempotency_key` | `conversations.history include_all_metadata=true` | history at `ts`, compare text | built (mocked HTTP) |
| Microsoft Teams | key in hidden card metadata; record the returned message id | list recent channel messages, match key | GET message | plan + confirm |
| Xero invoices | `Idempotency-Key` header | retry with the same key returns the original | GET invoice | plan |
| QuickBooks Online | `requestid` query parameter | same request id returns the original | GET invoice | plan |
| Stripe | `Idempotency-Key` header | same key returns the original | GET object | plan |

## Inbound

- **Webhooks.** Verify the provider's signature (HubSpot v3 signature, Slack signing secret, Stripe
  signature, or our own `X-Switchyard-Signature` HMAC for forms). Deduplicate on `(source, event_id)`.
  Respond 202 straight away and do the work in the worker. Built for the generic endpoint
  (`/webhooks/inbound`, `/webhooks/survey-completed`).
- **Email intake.** Use Gmail push (Pub/Sub watch) or Graph subscriptions, then fetch the message and
  submit it to `/webhooks/inbound` with the provider message id as the event id.
- **Polling fallback.** Use cursor-based polling with the provider's change token (Gmail `historyId`,
  Graph delta links). The cursor is stored, so a restart neither replays nor skips.

## OAuth and secrets

- Use one service principal or bot per integration, with the narrowest scopes the workflows need. The
  scopes are listed per action in the integration ticket.
- Refresh tokens are kept in the secret store, never in the database or in prompts. Adapters receive an
  access token from a small token provider that refreshes it.
- Agents never see tokens. The MCP server needs read scopes only.

## MCP

- `switchyard.mcp_server` exposes read-only tools over stdio (used by the agents) or streamable HTTP
  (for the MCP inspector, ChatGPT or Claude desktop). Each tool declares all four MCP hints
  (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`), so a host can see they change nothing.
- A client that already has MCP servers for its systems (for example a HubSpot or Google Drive MCP server)
  can plug them into the agents with the same per-agent `tool_filter`, keeping the rule that **writes go
  through the outbox**. Where a third-party MCP server only offers write tools, those tools are not given to
  agents; the matching adapter is used instead.
- OpenAI-hosted MCP tools (`HostedMCPTool`) suit read-only research tools. They are not used for writes,
  because the executor cannot enforce approval on calls the model makes directly.
