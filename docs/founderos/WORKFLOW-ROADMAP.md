# Founder workflow closure program

This program is derived from a bounded review of recent Linear work, calendar structure, Slack activity, and email follow-ups. It records general workflow patterns only. Private messages, customer data, and personal identifiers do not belong in the repository.

## Product model

FounderOS now treats the durable unit of work as an obligation, not an unread item. Every obligation records an owner, counterparty, next actor, due date, project, relationship, operational gates, evidence, state, source observations, and an append-only transition audit. Connectors still emit normalized events only. The deterministic closure engine correlates and governs those events before ranking.

The normal loop makes no LLM call. A model remains an optional, bounded fallback for a genuinely close tie.

## Implemented closure capabilities

1. **Commitment ledger**
   Outgoing Gmail promises, Slack promises, waits, decisions, Linear work, meeting transitions, and operator corrections become persistent obligations. Separate email and Slack threads remain separate commitments unless an explicit project key links them.

2. **Operational gates and false-ready detection**
   Release obligations distinguish code, deployment, access, evidence, and validation. Commitment, feedback, meeting, capacity, and decision profiles use their own gates. A newer source regression can reopen a formerly satisfied gate. A source timestamp or lease change alone cannot reopen a manually closed obligation. Open obligations never disappear because an observation ages out, while source evidence does expire and is reevaluated without requiring a new poll result.

3. **Capacity and availability**
   The engine detects due-date concentration by owner, Calendar and Home Assistant unavailability, and missing handoffs. Operators can record a delegate. A valid delegate satisfies the handoff instead of producing a false `NO BACKUP` alert.

4. **Burst compaction and priority normalization**
   Cross-source changes sharing an outcome key collapse into one obligation. Source priority is capped before closure context is added, and large update bursts cannot dominate the device merely because every source marked itself urgent.

5. **Meeting transitions**
   Important meetings enter a deterministic pre-meeting state. The same meeting identity moves to a post-meeting next-action state even when attendees share a customer domain. Routine scheduled meetings do not create obligations. Operators can record the concrete next action and its holder immediately.

6. **Relationship memory**
   Customer and partner records retain stage, last meaningful interaction, next decision, open obligations, resume date, and cooling-off date. Follow-ups are deferred during a cooling period unless a blocker or overdue commitment overrides it.

7. **Customer feedback to roadmap**
   Slack, Notion, and Sheets feedback is correlated with project and customer keys. Only feedback without an owner or recorded decision remains actionable.

8. **Evidence quorum**
   Release proof can require categories such as deployment, analytics, market, language, pricing, and device. Each category can also require configured scopes, for example both `market:FR` and `market:ES`. New contradictory source state retracts older evidence from the same observation.

## Implemented connector order

All adapters below are production code, bounded by poll deadlines and disabled until explicitly configured:

1. Notion, Google Drive, and Google Sheets for decisions, documents, review matrices, and scoped proof.
2. GitHub and generic deployment status for code, review, and release gates. Only configured deployment workflow names count as deployment proof.
3. Sentry and PostHog for regressions and analytics evidence.
4. Shopify for access, catalog, and merchant readiness.
5. A Gmail-backed Superhuman reminder bridge for an explicitly configured reminder label or query.
6. Stripe for overdue invoices, actionable disputes, and resolved financial evidence.
7. Home Assistant for opt-in availability context only.

Linear, Calendar, Slack, and Gmail remain the live default sources. Slack now reads bounded thread replies. Gmail reads incoming actions and an explicit outgoing-promise query. The additional connectors stay disabled until their least-privilege credentials, entity mappings, and live preflight are available.

## Operator surfaces

The emulator includes a local-only **Obligations** tab backed by the private, atomically published closure snapshot. The CLI provides audited corrections:

```bash
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation list
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation show OBLIGATION_ID
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation action OBLIGATION_ID "Send the validated proposal" --actor Yann
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation delegate OBLIGATION_ID Sam
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation gate OBLIGATION_ID validation satisfied --detail "Accepted by customer"
python3 apps/founderosctl.py --config founderos.autonomous.local.json obligation evidence OBLIGATION_ID market --scope FR --detail "Production check passed"
python3 apps/founderosctl.py --config founderos.autonomous.local.json relationship show partner.example
python3 apps/founderosctl.py --config founderos.autonomous.local.json relationship set partner.example --stage design_partner --next-decision "Approve rollout" --cooling-off-until 2026-08-20T08:00:00+02:00
```

## Activation gates, not code backlog

- Map the real Notion databases, Drive folders, Sheets ranges, repositories, deployment endpoint, Sentry projects, and PostHog checks before enabling their connectors.
- Define required evidence scopes for each real release, since FounderOS cannot safely invent markets, languages, prices, or devices.
- Use a real Superhuman-synchronized Gmail label or replace its query with the user's actual reminder convention.
- Keep Shopify, Stripe, and Home Assistant disabled until their narrow tokens and entity allowlists are provisioned.
- Complete the physical BUSY Bar acceptance window separately. Emulator compatibility cannot prove a particular device and firmware instance.

## Ranking principles

- Prefer an unresolved obligation over an unread notification.
- Prefer a customer or production commitment over internal backlog volume.
- Prefer the next owner transition over repeated copies of the same request.
- Increase urgency at meeting, travel, contractual, and launch boundaries.
- Preserve stale-source health explicitly. Never turn unavailable data into `ALL CLEAR`.
- Keep every score and gate deterministic and auditable.
