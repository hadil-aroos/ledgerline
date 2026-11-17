# Ledgerline

A free, self-hosted, multi-user bookkeeping web app (Python + Flask + SQLite) that imports a company's
history from **QuickBooks Desktop** exports and keeps going from there. Several people sign in with their
own account at the same time from any browser.

> This repository contains the application only. No company data is included: your database, exports
> and settings stay on your own machine (see `.gitignore`).

## Features
- **Dashboard:** fiscal-year income, costs and net profit, receivables and payables, bank balances,
  a 12-month income/cost chart, quick actions and the latest transactions.
- **Global search and "+ New" menu** on every page.
- **Transactions:** searchable by name, number, memo, type, date and amount; each opens with its lines.
- **Chart of accounts** with balances as of any date, and an **account register** with running balance.
- **Customers, vendors, items:** lists with balances, contact details and history. Items can be added
  and edited.
- **Reports:** Profit & Loss, Balance Sheet, Trial Balance, receivables and payables aging. Print or CSV.
- **Recording:** invoices, received payments, bills, bill payments, cheques/expenses, journal entries.
  Every entry must balance.
- **Users and roles:** `viewer` (read only), `accountant` (can record), `admin` (manages users, can delete
  entries made in the app). Every sign-in and change goes to an activity log.
- CSRF protection on every form, hashed passwords, login rate limiting, HTML-escaped output.

Imported QuickBooks history is read-only; correct it with a journal entry.

## Setup
```
git clone <this repo> ledgerline && cd ledgerline
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt        # macOS/Linux: .venv/bin/pip ...
copy .env.example .env                                 # then edit company name, currency, fiscal year
```

### Import from QuickBooks Desktop
Put these exports in a `qb-export/` folder, then run `python importer.py`:

| File | How to export in QuickBooks Desktop |
|---|---|
| `QBX_Accounts.CSV` | Reports → List → Account Listing → Excel → .csv |
| `QBX_Customers.CSV` | Customer Center → Excel → Export Customer List → .csv |
| `QBX_Vendors.CSV` | Vendor Center → Excel → Export Vendor List → .csv |
| `QBX_Items.CSV` | Item List → Excel → Export All Items → .csv |
| `*Journal*Full*.xlsx` | Reports → Accountant & Taxes → Journal, Dates **All** → Excel → new workbook, save as .xlsx (CSV exports stop at 32,768 rows) |
| `QBX_TD_Fixed.CSV` | Transaction Detail by Account, Dates All, add the **Trans #** column, filter Account = *All fixed assets* → .csv |
| `QBX_TD_OCL.CSV` | Same report, filter Account = *All other current liabilities* → .csv |

The two Transaction Detail files are optional. QuickBooks' journal shows only the last part of each
account name, so same-named sub-accounts (e.g. *Accumulated Depreciation:Vehicles* and *Property, Plant
& Equipment:Vehicles*) collide; those reports let the importer put every line in the right account.
Re-running the importer replaces imported history but keeps users and everything entered in the app.

### Run
```
python manage.py adduser <name> admin     # asks for a password
python serve.py                           # or start.bat on Windows
```
Open http://127.0.0.1:8080. Add other people under **Users & activity**.

## Sharing with a team
`serve.py` listens on port 8080 on all interfaces. On the same network, others open
`http://<this machine's IP>:8080`. For access over the internet, run it on an always-on server behind
HTTPS (Caddy or a Cloudflare Tunnel) and set `BOOKS_HTTPS=1`. Never expose it over plain HTTP.

## Settings (`.env`)
`BOOKS_COMPANY`, `BOOKS_CURRENCY`, `BOOKS_FY_START_MONTH`, `BOOKS_DEFAULT_INCOME_ACCOUNT`,
`BOOKS_DEFAULT_COGS_ACCOUNT`, `BOOKS_HTTPS`, `PORT`, `HOST`, `BOOKS_DB`.

## Backups
All data is in `books.db` (SQLite). Copy it (with `.secret_key`) while the app is stopped, or on a schedule.

## Tests
`python test_app.py` opens every page and posts test entries against a temporary copy of `books.db`.
