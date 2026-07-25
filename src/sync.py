"""Entrypoint for the ServiceTitan to QuickBooks Online sync.

Runs a single reconciliation pass over a time window. Safe to rerun over the same
window: writes are idempotent by ServiceTitan invoice ID.

Usage:
    python -m src.sync --since 24h
    python -m src.sync --since 7d --dry-run
    python -m src.sync --invoice 887766  # single invoice replay
"""

import argparse
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from src.clients.servicetitan import ServiceTitanClient
from src.clients.quickbooks import QuickBooksClient
from src.mapping.accounts import load_account_mapping
from src.reconcile.invoices import reconcile_invoice
from src.reconcile.payments import reconcile_payment
from src.state.ledger import Ledger

log = logging.getLogger("avalux.sync")

_WINDOW = re.compile(r"^(\d+)([hd])$")


def parse_window(spec: str) -> timedelta:
    m = _WINDOW.match(spec.strip().lower())
    if not m:
        raise ValueError(f"invalid window '{spec}'. use forms like 24h or 7d.")
    n, unit = int(m.group(1)), m.group(2)
    return timedelta(hours=n) if unit == "h" else timedelta(days=n)


def run(since: timedelta, dry_run: bool, single_invoice_id: int | None) -> int:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("SYNC_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    st = ServiceTitanClient.from_env()
    qbo = QuickBooksClient.from_env()
    mapping = load_account_mapping(os.getenv("SYNC_ACCOUNT_MAPPING_PATH"))
    ledger = Ledger(os.getenv("SYNC_LEDGER_PATH", "state/ledger.db"))

    if single_invoice_id is not None:
        invoices = [st.get_invoice(single_invoice_id)]
        payments = st.list_payments_for_invoice(single_invoice_id)
    else:
        cutoff = datetime.now(timezone.utc) - since
        log.info("reconciling invoices modified since %s (dry_run=%s)", cutoff.isoformat(), dry_run)
        invoices = list(st.list_invoices_modified_since(cutoff))
        payments = list(st.list_payments_modified_since(cutoff))

    posted = 0
    skipped = 0
    errored = 0

    for inv in invoices:
        try:
            result = reconcile_invoice(inv, st=st, qbo=qbo, mapping=mapping, ledger=ledger, dry_run=dry_run)
            if result.action == "skipped":
                skipped += 1
            else:
                posted += 1
                log.info("invoice ST-%s %s -> QBO %s", inv.id, result.action, result.qbo_id or "(dry)")
        except Exception as exc:  # bubble specific reasons into the log
            errored += 1
            log.exception("invoice ST-%s failed: %s", inv.id, exc)

    for pay in payments:
        try:
            reconcile_payment(pay, st=st, qbo=qbo, ledger=ledger, dry_run=dry_run)
        except Exception as exc:
            errored += 1
            log.exception("payment ST-%s failed: %s", pay.id, exc)

    log.info("done. posted=%d skipped=%d errored=%d", posted, skipped, errored)
    return 0 if errored == 0 else 2


def main() -> int:
    parser = argparse.ArgumentParser(description="ServiceTitan -> QuickBooks Online sync")
    parser.add_argument("--since", default="24h", help="time window, e.g. 24h or 7d")
    parser.add_argument("--dry-run", action="store_true", help="compute writes without posting to QBO")
    parser.add_argument("--invoice", type=int, default=None, help="replay a single ServiceTitan invoice by ID")
    args = parser.parse_args()

    return run(
        since=parse_window(args.since),
        dry_run=args.dry_run,
        single_invoice_id=args.invoice,
    )


if __name__ == "__main__":
    sys.exit(main())
