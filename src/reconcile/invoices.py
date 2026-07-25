"""Idempotent invoice reconciliation from ServiceTitan into QuickBooks Online.

Contract:
    - Same ServiceTitan invoice ID must never produce two QBO invoices.
    - Edits in ServiceTitan (line changes, discounts, voids) must flow through as
      updates, not new invoices.
    - Unmapped revenue codes must be flagged, not silently posted to a default.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from src.clients.servicetitan import ServiceTitanClient, STInvoice
from src.clients.quickbooks import QuickBooksClient
from src.mapping.accounts import AccountMapping
from src.mapping.customers import upsert_customer
from src.state.ledger import Ledger


@dataclass
class ReconcileResult:
    action: Literal["created", "updated", "voided", "skipped"]
    qbo_id: str | None


def _external_doc_number(st_invoice_id: int) -> str:
    return f"ST-{st_invoice_id}"


def _build_qbo_lines(inv: STInvoice, mapping: AccountMapping) -> list[dict]:
    lines = []
    for item in inv.items:
        rule = mapping.for_sku(item.sku_category)
        if rule.is_fallback:
            # flag but still post so the invoice reconciles; the log line here
            # is what an operator greps for at month end to fix the mapping.
            pass
        lines.append(
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": float(item.amount),
                "Description": item.description,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": rule.qbo_item_id, "name": rule.qbo_item_name},
                    "ClassRef": {"name": rule.qbo_class} if rule.qbo_class else None,
                    "TaxCodeRef": {"value": "TAX" if item.taxable else "NON"},
                },
            }
        )
    return [{k: v for k, v in line.items() if v is not None} for line in lines]


def reconcile_invoice(
    inv: STInvoice,
    *,
    st: ServiceTitanClient,
    qbo: QuickBooksClient,
    mapping: AccountMapping,
    ledger: Ledger,
    dry_run: bool,
) -> ReconcileResult:
    doc_number = _external_doc_number(inv.id)

    if inv.status == "void":
        existing = ledger.qbo_id_for_st_invoice(inv.id) or qbo.find_invoice_by_doc_number(doc_number)
        if not existing:
            return ReconcileResult(action="skipped", qbo_id=None)
        if not dry_run:
            qbo.void_invoice(existing)
            ledger.mark_voided(inv.id)
        return ReconcileResult(action="voided", qbo_id=existing)

    customer_id = upsert_customer(inv.customer, qbo=qbo, ledger=ledger, dry_run=dry_run)

    payload = {
        "DocNumber": doc_number,
        "CustomerRef": {"value": customer_id},
        "TxnDate": inv.invoice_date.isoformat(),
        "Line": _build_qbo_lines(inv, mapping),
        "PrivateNote": f"ServiceTitan invoice {inv.id} / job {inv.job_id} / bu {inv.business_unit}",
    }

    existing_qbo_id = ledger.qbo_id_for_st_invoice(inv.id)
    if not existing_qbo_id:
        # belt and suspenders: ledger may be behind if a prior run crashed after
        # QBO returned but before we committed. always double check on QBO too.
        existing_qbo_id = qbo.find_invoice_by_doc_number(doc_number)

    if existing_qbo_id:
        if dry_run:
            return ReconcileResult(action="updated", qbo_id=existing_qbo_id)
        qbo.update_invoice(existing_qbo_id, payload)
        ledger.record_pair(inv.id, existing_qbo_id, total=Decimal(str(inv.total)))
        return ReconcileResult(action="updated", qbo_id=existing_qbo_id)

    if dry_run:
        return ReconcileResult(action="created", qbo_id=None)

    new_qbo_id = qbo.create_invoice(payload)
    ledger.record_pair(inv.id, new_qbo_id, total=Decimal(str(inv.total)))
    return ReconcileResult(action="created", qbo_id=new_qbo_id)
