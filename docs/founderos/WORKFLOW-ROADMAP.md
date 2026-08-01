# Founder workflow roadmap

This roadmap is derived from a bounded review of recent Linear work, calendar structure, Slack activity, and email follow-ups. It records general workflow patterns only. Private messages, customer data, and personal identifiers do not belong in the repository.

## What the founder workflow actually contains

The dominant unit of work is not an assigned task. It is a decision or dependency that moves across several systems:

1. A launch or customer outcome is represented as a Linear project and deadline.
2. Product quality is verified manually in production, often across markets, languages, devices, and analytics tools.
3. A technical decision or fix is requested in Slack or email.
4. The founder waits, follows up, escalates, or validates the result.
5. A calendar meeting becomes the next decision boundary.
6. Legal, billing, and administrative requests compete with product and sales work.

FounderOS should therefore rank decision debt, critical-path risk, follow-up promises, and meeting readiness. Counting unread items is not enough.

## P0, build next

### Decision debt detector

Detect explicit requests that remain unresolved, record who owns the next move, count follow-ups, and increase urgency as the waiting time grows. Collapse repeated reminders into one event such as `Engineering decision blocked for 2 days`.

### Critical-path rollup

Roll up a Linear epic, its open children, owners, due dates, and blocked states into one founder-facing event. The title should state the outcome at risk, not repeat an issue title.

### Meeting readiness mode

Thirty minutes before an important meeting, replace generic reminders with a compact brief assembled from the related Linear project, latest Slack decision, and unresolved email promise. After the meeting, suppress it and surface the next recorded commitment.

### Promise and follow-up radar

Recognize commitments such as `I will send`, `waiting for`, `please confirm`, and `come back by`. Track whether the promised artifact or answer arrived, then distinguish `do now`, `waiting`, and `safe to ignore` deterministically.

### Runway mode

Before travel, leave, or a dense meeting block, rank only work that must be closed, delegated, or explicitly deferred before the cutoff. During leave, suppress routine noise and permit only incidents, contractual deadlines, and launch-critical blockers.

## P1, make the system useful every day

### Cross-source entity graph

Link projects, customers, teammates, contracts, and meetings across systems. One underlying situation should create one event even when it appears in four connectors.

### Verification evidence

Attach compact proof to quality-gate events: environments checked, expected result, observed result, owner, and latest deployment. This turns a vague `please test` notification into an auditable release decision.

### Physical acknowledge and snooze

Permission requests now establish the safe input pattern: one selected event, one explicit `OK` or `BACK` decision, one-use local state, and a short timeout. The emulator input adapter is complete. General acknowledge, snooze, and open-source-link actions remain to be added, along with an official outbound button transport for physical hardware. External writes should remain explicit and separate.

### Daily transition briefs

Offer deterministic modes for start of day, before a meeting, end of day, and return from leave. Each mode should still select one action, with optional secondary context on the back display or companion UI.

### Contact and account memory

Track the current stage, open promise, last meaningful exchange, next decision, and cooling-off date for each customer or partner. This should remain a compact operational memory, not a second CRM.

## P2, expand after the core loop proves useful

- GitHub and deployment signals for regressions that threaten a committed outcome.
- PostHog and Sentry signals tied to the relevant Linear release gate.
- Stripe and accounting reminders for invoices, refunds, and month-end evidence.
- Shopify merchant-readiness checks for required permissions, data completeness, and launch prerequisites.
- Home Assistant context only for personal availability and focus protection, disabled by default.

## Connector order inferred from the workflow

After Linear, Calendar, Slack, and Gmail, the highest-value additions are:

1. Google Drive and Sheets, for decision registers, proposals, review matrices, and promised deliverables.
2. Notion, for the latest approved product, architecture, tracking, and operating decisions.
3. PostHog and Sentry, for production evidence and regressions tied to a release gate.
4. Shopify, for merchant permissions, catalog freshness, integration readiness, and launch health.
5. GitHub and deployment status, for code reviews, failed checks, and releases affecting a customer commitment.
6. Superhuman reminder state, as an enrichment of Gmail follow-up and waiting signals rather than a duplicate inbox.
7. Stripe and accounting evidence, once product and customer decisions are reliably ranked.

LinkedIn remains useful for relationship follow-up, but it should rank below explicit customer commitments already present in email. Home Assistant should remain last and opt-in because it rarely changes the business decision itself.

## Ranking principles

- Prefer an unresolved decision over an unread notification.
- Prefer a customer or production commitment over internal backlog volume.
- Prefer the next actionable owner transition over repeated copies of the same request.
- Increase urgency at meeting, travel, contractual, and launch boundaries.
- Decay stale connector data aggressively.
- Keep the normal loop deterministic and explain every score component.
- Use an LLM only to summarize a long thread or resolve a genuinely close tie.
