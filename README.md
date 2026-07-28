# servicetitan-quickbooks-sync

> Open source scaffolding for syncing ServiceTitan invoices, payments, and journal entries into QuickBooks Online. Built and maintained by [Avalux](https://avalux.io), a full-stack AI automation studio for operators. Part of our [open source toolkit](https://github.com/avalux-io/avalux-open-source).

Home services shops on ServiceTitan run their operations in one system and their books in another. Techs close out a job in the field, the invoice hits ServiceTitan, and then somebody, usually the office manager, retypes it into QuickBooks Online at the end of the week. When there are three locations, four service lines, and a technician on commission, the retyping stops being a small task and becomes the reason the books close a month late.

This repository is the working scaffold we start from when a client asks us to eliminate that retyping. It talks to the ServiceTitan Tenant API on one side, the QuickBooks Online API on the other, and moves invoice, payment, and adjustment data between them with idempotent replay so nothing gets double-posted when a webhook fires twice or a nightly cron overlaps a manual sync.

## Why this exists

Every ServiceTitan to QuickBooks Online integration we have seen in production dies in the same place. The first version handles the happy path, invoice created in ServiceTitan, invoice created in QuickBooks, everyone celebrates. Then a customer calls three days later, the office edits the ServiceTitan invoice to add a discount, and now the books are out of sync in a way nobody notices for six weeks.

The second failure mode is worse. Somebody wires up a middleware tool with default field mappings, all revenue lands in a single Sales account, and by the time the CPA asks why there is no way to split HVAC service from plumbing installs, there are eighteen thousand invoices already booked wrong.

We built this to give operators, and the developers they hire, a starting point that assumes both of those problems from day one. Invoices carry a stable external ID so replays are safe. Line items map to real chart of accounts entries, not a single lump. Business units in ServiceTitan can be routed to separate QuickBooks classes or locations. Payments reference the original invoice by ServiceTitan invoice ID rather than by matching on customer name and amount, which is what most quick integrations fall back on and what breaks when two customers pay the same amount on the same day.

This is not a hosted product. This is the codebase we wish existed on day one of every ServiceTitan to QuickBooks Online project.

## Who this is for

This repo is aimed at three groups.

Internal engineering teams at multi-location home services companies, HVAC, plumbing, electrical, roofing, running on ServiceTitan who want to own their accounting integration rather than pay per invoice for a middleware SaaS. If you have a Python developer on staff or on retainer, this gets you from zero to a working staging environment in a day or two, and the customization from there is straightforward.

Agencies and consultants building on ServiceTitan for their home services clients. Rather than starting each engagement from a blank file, fork this, replace the tenant credentials, adjust the account mapping to the client's chart of accounts, and ship. The MIT license permits commercial use, including as part of a paid deliverable.

Founders evaluating whether to build in-house or buy a connector. The code here is annotated enough that a non-Python-first CTO can read the reconciliation logic and make an informed call. If you decide to buy, at least you know what you are buying.

This is not a plug-and-play SaaS. There is no admin UI. There is no hosted webhook receiver. If you need those, [get in touch with Avalux](https://avalux.io) and we can build the wrapper. What is in the repo is the hard part, the domain logic, the API glue, the reconciliation math.

## What is in the box

```
servicetitan-quickbooks-sync/
├── src/
│   ├── sync.py                  # main sync loop, entrypoint
│   ├── clients/
│   │   ├── servicetitan.py      # ServiceTitan Tenant API client
│   │   └── quickbooks.py        # QuickBooks Online API client
│   ├── mapping/
│   │   ├── accounts.py          # ServiceTitan revenue codes to QBO accounts
│   │   ├── customers.py         # customer upsert with dedup rules
│   │   ├── items.py             # ServiceTitan SKUs to QBO items
│   │   └── locations.py         # business unit to QBO class or location
│   ├── reconcile/
│   │   ├── invoices.py          # idempotent invoice sync
│   │   ├── payments.py          # payment application by invoice ref
│   │   └── adjustments.py       # credit memos and voids
│   ├── state/
│   │   └── ledger.py            # local SQLite ledger of external ID pairs
│   └── auth/
│       ├── servicetitan_token.py
│       └── qbo_token.py         # OAuth 2 refresh handling
├── config/
│   └── account_mapping.example.yaml
├── tests/
│   └── test_reconcile.py
├── .env.example
├── requirements.txt
└── README.md
```

The piece to read first is `src/reconcile/invoices.py`. That is where the idempotency lives, and if you understand what it is doing, the rest of the codebase reads straightforwardly.

## Quick start

This assumes Python 3.11 or newer, a ServiceTitan tenant with API access enabled, and a QuickBooks Online company file with a developer app configured for OAuth 2.

```bash
git clone https://github.com/avalux-io/servicetitan-quickbooks-sync.git
cd servicetitan-quickbooks-sync
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
cp config/account_mapping.example.yaml config/account_mapping.yaml
```

Fill in `.env` with your ServiceTitan tenant ID, client ID, client secret, application key, and your QuickBooks Online realm ID, client ID, client secret, and refresh token. The `.env.example` file lists all of them with notes on where each one is found in the respective developer portals.

Then do a dry run against the last twenty four hours of ServiceTitan invoices:

```bash
python -m src.sync --since 24h --dry-run
```

Dry run mode reads from ServiceTitan, computes what would post to QuickBooks Online, and prints the intended writes without hitting the QBO API. Review the output. When it looks right, drop the `--dry-run` flag.

## How the sync actually works

The write path from ServiceTitan into QuickBooks Online has four moving parts and each one has to be right or the books drift.

**Idempotent external IDs.** Every ServiceTitan invoice has a numeric ID. When we create the corresponding QuickBooks Online invoice, we store the ServiceTitan ID in the QBO invoice's DocNumber field, prefixed with `ST-`, and we also store the pair in a local SQLite ledger at `state/ledger.db`. Before we write anything, we check both the ledger and, as a belt-and-suspenders check, we query QBO for an invoice with that DocNumber. If either check hits, we update rather than insert. This is what makes replay safe. You can rerun the sync for the same window ten times in a row and no invoice will be duplicated.

**Chart of accounts mapping.** ServiceTitan gives you invoice line items with a SKU and a business unit. Neither of those is a QuickBooks account. The mapping lives in `config/account_mapping.yaml` and it looks like this:

```yaml
revenue_accounts:
  HVAC-SERVICE:
    account: "Sales:HVAC Service Revenue"
    class: "HVAC"
  HVAC-INSTALL:
    account: "Sales:HVAC Install Revenue"
    class: "HVAC"
  PLUMB-SERVICE:
    account: "Sales:Plumbing Service Revenue"
    class: "Plumbing"

business_units:
  "Dallas HVAC":
    qbo_location: "Dallas"
  "Fort Worth HVAC":
    qbo_location: "Fort Worth"

defaults:
  fallback_account: "Sales:Uncategorized Revenue"
  fallback_class: "Unassigned"
  tax_account: "Sales Tax Payable"
```

Any SKU category that is not explicitly mapped falls into the fallback account and gets flagged in the sync log so somebody can go back and add the mapping. This is deliberate. The alternative is silently booking new revenue types to the wrong account, which is how you end up rebuilding a chart of accounts from scratch six months in.

**Multi-location handling.** ServiceTitan business units map either to QuickBooks Online classes, locations, or both. QBO's location tracking is only available on Plus and Advanced plans. The client detects which tier the connected company file is on and falls back to class tracking with a naming convention, or, if neither is enabled, appends the business unit to the invoice memo so at least it is queryable.

**Payment application.** Payments are the trickiest part because ServiceTitan and QuickBooks Online model them differently. In ServiceTitan a payment can apply to multiple jobs across multiple invoices. In QBO a payment applies to one or more invoices for a single customer. The reconciler in `src/reconcile/payments.py` breaks a ServiceTitan payment into one QBO payment per customer, links each one to the QBO invoices that correspond to the ServiceTitan invoices it applied to, and if any of those QBO invoices don't exist yet, the payment is queued until the next run and retried. This is what keeps AR balances consistent without requiring a fixed ordering between the invoice sync and the payment sync.

## What we deliberately left out

Inventory sync. ServiceTitan's inventory model does not translate cleanly to QuickBooks Online's, and any real implementation needs a conversation with the operator about how they want stock accounted for. We do not want to bake in the wrong assumption.

Job costing. QBO's Projects feature can approximate ServiceTitan job costing but only for the Plus and Advanced tiers and only if the operator wants to run their P&L that way. Out of scope for the scaffold.

Payroll and technician commission. Commission is calculated in ServiceTitan and flows to whatever payroll system the operator uses, which is almost never QuickBooks Payroll. Not a sync problem.

A hosted webhook receiver. The current design polls ServiceTitan on a schedule. If you want to move to real-time webhook-driven sync you will need to add an HTTP endpoint in front of the reconciler and handle ServiceTitan's signature verification, which is not hard but is deployment-specific.

## Why we built this

Avalux is an AI and automation studio for operators. Most of what we do is not open source, we build custom internal tooling and data pipelines for clients where the specifics are proprietary, but the boring plumbing under those projects is the same everywhere. Publishing the plumbing lets us start engagements faster, and it lets founders evaluate our work without a sales call.

If you are running a home services business on ServiceTitan and the accounting sync is the thing that keeps breaking, or if you looked at this repo and thought "this is 40 percent of what I need, could someone finish it," that is exactly the shape of work we take. [Reach out at avalux.io](https://avalux.io).

Everything in this repository is a starting point. Real production use requires connecting your specific ServiceTitan tenant, mapping your actual chart of accounts, testing against a QuickBooks Online sandbox for a full billing cycle, and then hardening the parts that fail. That last step, hardening, is where most integrations that go into production without an engineering owner eventually die. Budget for it.

## Avalux's other open source projects

- [freight-eta-toolkit](https://github.com/avalux-io/freight-eta-toolkit) - ETA calculation and geofencing for freight and last-mile operations.
- [avalux-open-source](https://github.com/avalux-io/avalux-open-source) - Meta-repo indexing everything we have published.

More in the pipeline. Follow [@avalux_io](https://avalux.io) for what we publish next.

## License

MIT. See [LICENSE](./LICENSE). Use it in a commercial product, fork it, rebrand it, we do not mind. If it saves you a week of work we would love to hear about it.

## Keywords

ServiceTitan QuickBooks integration, ServiceTitan QuickBooks Online sync, ServiceTitan QBO sync, ServiceTitan accounting integration, HVAC accounting software integration, plumbing accounting sync, field service accounting automation, ServiceTitan invoice sync, ServiceTitan payment sync, QuickBooks Online API Python, ServiceTitan Tenant API, home services accounting automation, multi-location field service accounting, chart of accounts mapping ServiceTitan, idempotent invoice replay, ServiceTitan business unit QuickBooks class, open source ServiceTitan connector, ServiceTitan CPA integration, home services QuickBooks integration.
