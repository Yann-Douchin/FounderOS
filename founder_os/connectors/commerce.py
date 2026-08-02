"""Read-only Shopify readiness and Stripe obligation connectors."""

from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any, Mapping

from founder_os.connectors.base import Connector, ConnectorConfigurationError, ConnectorError, configured_secret
from founder_os.connectors.http_client import request_json
from founder_os.models import Event, parse_datetime


SHOPIFY_READINESS_QUERY = """
query FounderOSReadiness {
  shop { name primaryDomain { url } }
  productsCount { count precision }
}
"""


class ShopifyConnector(Connector):
    name = "shopify"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.shop = str(config.get("shop") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", self.shop):
            raise ConnectorConfigurationError("shopify.shop must be a myshopify.com hostname")
        self.api_version = str(config.get("api_version", "2026-07"))
        self.endpoint = str(
            config.get("endpoint", f"https://{self.shop}/admin/api/{self.api_version}")
        ).rstrip("/")
        self.required_scopes = {str(value) for value in config.get("required_scopes", ["read_products"]) if str(value)}
        self.minimum_products = max(0, int(config.get("minimum_products", 1)))
        self.project = str(config.get("project") or self.shop.split(".", 1)[0])
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 8)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        headers = {"X-Shopify-Access-Token": self.token}
        scopes_payload = request_json(
            f"{self.endpoint}/oauth/access_scopes.json",
            headers=headers,
            timeout=min(self.request_timeout, max(0.1, deadline - time.monotonic())),
            retries=0,
            deadline_monotonic=deadline,
        )
        access_scopes = scopes_payload.get("access_scopes") or []
        if not isinstance(access_scopes, list):
            raise ConnectorError("Shopify response did not contain access scopes")
        granted = {str(item.get("handle") or "") for item in access_scopes if isinstance(item, Mapping)}
        graphql = request_json(
            f"{self.endpoint}/graphql.json",
            method="POST",
            body={"query": SHOPIFY_READINESS_QUERY},
            headers=headers,
            timeout=min(self.request_timeout, max(0.1, deadline - time.monotonic())),
            retries=0,
            deadline_monotonic=deadline,
        )
        if graphql.get("errors"):
            raise ConnectorError("Shopify GraphQL response contained errors")
        data = graphql.get("data") or {}
        if not isinstance(data, Mapping):
            raise ConnectorError("Shopify GraphQL response did not contain data")
        product_count = _int(((data.get("productsCount") or {}).get("count")), 0)
        shop_name = str((data.get("shop") or {}).get("name") or self.project)
        shop_url = str((((data.get("shop") or {}).get("primaryDomain") or {}).get("url")) or f"https://{self.shop}")
        missing_scopes = sorted(self.required_scopes - granted)
        problems = []
        if missing_scopes:
            problems.append("missing scopes: " + ", ".join(missing_scopes))
        if product_count < self.minimum_products:
            problems.append(f"catalog has {product_count} products")
        ready = not problems
        return [Event(
            id=f"shopify:{self.shop}:readiness",
            source="shopify",
            title=f"{shop_name} | {'MERCHANT READY' if ready else 'READINESS BLOCKED'}",
            body="; ".join(problems) if problems else f"{product_count} products and required scopes present",
            kind="blocker" if not ready else "information",
            priority=88 if not ready else 34,
            action_required=not ready,
            urgency="high" if not ready else "normal",
            impact="high",
            occurred_at=now,
            expires_at=now + timedelta(hours=2),
            dedupe_key=f"shopify:{self.shop}:readiness",
            url=shop_url,
            metadata={
                "project": self.project,
                "customer": shop_name,
                "shop": self.shop,
                "product_count": product_count,
                "missing_scopes": missing_scopes,
                "access_status": "granted" if not missing_scopes else "missing",
                "gate_status": {"access": "satisfied" if not missing_scopes else "blocked"},
                "evidence_categories": ["merchant", "access"] if ready else [],
                "evidence_status": "present" if ready else "failed",
            },
        )]


class StripeConnector(Connector):
    name = "stripe"

    def __init__(self, config: Mapping[str, Any]) -> None:
        super().__init__(config)
        self.token = configured_secret(config, "token_env", self.secrets)
        self.endpoint = str(config.get("endpoint", "https://api.stripe.com/v1")).rstrip("/")
        self.page_size = min(100, max(1, int(config.get("page_size", 50))))
        self.request_timeout = max(1.0, float(config.get("request_timeout_seconds", 6)))

    def poll(self, now: datetime) -> list[Event]:
        deadline = time.monotonic() + self.poll_timeout_seconds
        headers = {"Authorization": f"Bearer {self.token}"}
        invoices = request_json(
            f"{self.endpoint}/invoices",
            query={"status": "open", "limit": self.page_size},
            headers=headers,
            timeout=min(self.request_timeout, max(0.1, deadline - time.monotonic())),
            retries=0,
            deadline_monotonic=deadline,
        )
        disputes = request_json(
            f"{self.endpoint}/disputes",
            query={"limit": self.page_size},
            headers=headers,
            timeout=min(self.request_timeout, max(0.1, deadline - time.monotonic())),
            retries=0,
            deadline_monotonic=deadline,
        )
        invoice_rows = invoices.get("data") or []
        dispute_rows = disputes.get("data") or []
        if not isinstance(invoice_rows, list) or not isinstance(dispute_rows, list):
            raise ConnectorError("Stripe response did not contain data lists")
        events = [self._invoice(item, now) for item in invoice_rows if isinstance(item, Mapping)]
        events.extend(self._dispute(item, now) for item in dispute_rows if isinstance(item, Mapping))
        return events

    @staticmethod
    def _invoice(item: Mapping[str, Any], now: datetime) -> Event:
        invoice_id = str(item.get("id") or "").strip()
        if not invoice_id:
            raise ConnectorError("Stripe invoice is missing its id")
        due_at = parse_datetime(item.get("due_date"))
        overdue = bool(due_at and due_at <= now)
        customer = str(item.get("customer_name") or item.get("customer_email") or item.get("customer") or "Customer")
        amount = _int(item.get("amount_due"), 0) / 100
        currency = str(item.get("currency") or "").upper()
        return Event(
            id=f"stripe:invoice:{invoice_id}",
            source="stripe",
            title=f"{customer} invoice {amount:g} {currency}",
            body="overdue" if overdue else "open invoice",
            kind="deadline",
            priority=84 if overdue else 62,
            action_required=overdue,
            urgency="critical" if overdue else "normal",
            impact="high" if overdue else "medium",
            occurred_at=parse_datetime(item.get("created"), default=now) or now,
            due_at=due_at,
            expires_at=now + timedelta(days=30),
            dedupe_key=f"stripe:invoice:{invoice_id}",
            url=str(item.get("hosted_invoice_url") or ""),
            metadata={
                "customer": customer,
                "relationship_key": str(item.get("customer_email") or item.get("customer") or ""),
                "amount_due": amount,
                "currency": currency,
                "evidence_categories": ["finance"] if not overdue else [],
                "evidence_status": "failed" if overdue else "present",
            },
        )

    @staticmethod
    def _dispute(item: Mapping[str, Any], now: datetime) -> Event:
        dispute_id = str(item.get("id") or "").strip()
        if not dispute_id:
            raise ConnectorError("Stripe dispute is missing its id")
        status = str(item.get("status") or "needs_response")
        action_required = status in {"needs_response", "warning_needs_response"}
        failed = status in {"lost", "warning_closed"}
        resolved = status in {"won", "prevented"}
        due_at = parse_datetime(((item.get("evidence_details") or {}).get("due_by")))
        return Event(
            id=f"stripe:dispute:{dispute_id}",
            source="stripe",
            title=f"Stripe dispute {status}",
            body=str(item.get("reason") or ""),
            kind="blocker" if action_required else "information",
            priority=94 if action_required else 36,
            action_required=action_required,
            urgency="critical" if action_required else "normal",
            impact="high",
            occurred_at=parse_datetime(item.get("created"), default=now) or now,
            due_at=due_at,
            expires_at=now + timedelta(days=30),
            dedupe_key=f"stripe:dispute:{dispute_id}",
            metadata={
                "evidence_status": "present" if resolved else "failed" if failed or action_required else "stale",
                "evidence_categories": ["finance", "validation"] if resolved else [],
                "gate_status": {"validation": "blocked"} if action_required else {"validation": "satisfied"} if resolved else {},
            },
        )


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
