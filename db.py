import os
import sqlite3

DB_PATH = os.environ.get("BOOKS_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    full_name TEXT,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin','accountant','viewer')),
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    at TEXT DEFAULT CURRENT_TIMESTAMP,
    username TEXT,
    action TEXT,
    detail TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT,
    qb_balance REAL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    company TEXT, contact TEXT, phone TEXT, alt_phone TEXT, email TEXT, address TEXT,
    customer_type TEXT, terms TEXT, rep TEXT,
    qb_balance REAL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS vendors (
    id INTEGER PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    company TEXT, contact TEXT, phone TEXT, address TEXT,
    qb_balance REAL DEFAULT 0,
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    full_name TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    type TEXT, description TEXT, income_account TEXT, cogs_account TEXT,
    cost REAL DEFAULT 0, price REAL DEFAULT 0, qty_on_hand REAL DEFAULT 0, unit TEXT,
    preferred_vendor TEXT, mpn TEXT,
    source TEXT NOT NULL DEFAULT 'app'
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    qb_trans_no INTEGER,
    type TEXT NOT NULL,
    date TEXT NOT NULL,
    num TEXT,
    name TEXT,
    memo TEXT,
    source TEXT NOT NULL DEFAULT 'app',
    created_by TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS splits (
    id INTEGER PRIMARY KEY,
    trans_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    name TEXT,
    memo TEXT,
    debit REAL NOT NULL DEFAULT 0,
    credit REAL NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS ix_splits_account ON splits(account_id);
CREATE INDEX IF NOT EXISTS ix_splits_trans ON splits(trans_id);
CREATE INDEX IF NOT EXISTS ix_splits_name ON splits(name);
CREATE INDEX IF NOT EXISTS ix_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS ix_tx_name ON transactions(name);
CREATE INDEX IF NOT EXISTS ix_tx_type ON transactions(type);
"""

# Account type groups (QuickBooks Desktop type names).
ASSET_TYPES = ("Bank", "Accounts Receivable", "Other Current Asset", "Fixed Asset", "Other Asset")
LIABILITY_TYPES = ("Accounts Payable", "Credit Card", "Other Current Liability", "Long Term Liability")
EQUITY_TYPES = ("Equity",)
INCOME_TYPES = ("Income", "Other Income")
COGS_TYPES = ("Cost of Goods Sold",)
EXPENSE_TYPES = ("Expense", "Other Expense")
CREDIT_NORMAL = LIABILITY_TYPES + EQUITY_TYPES + INCOME_TYPES
ALL_TYPES = ASSET_TYPES + LIABILITY_TYPES + EQUITY_TYPES + INCOME_TYPES + COGS_TYPES + EXPENSE_TYPES + ("Non-Posting",)


def connect(path=None):
    con = sqlite3.connect(path or DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def init(con):
    con.executescript(SCHEMA)
    con.commit()
