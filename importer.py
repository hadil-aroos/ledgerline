"""Import QuickBooks Desktop exports (CSV lists + full Journal .xlsx) into books.db (SQLite).

Usage:  python importer.py [export_folder]      (default: qb-export)

Re-running rebuilds the imported data. Users and entries created in the app are kept.
"""
import csv
import glob
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime

import db

EXPORT_DIR = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "qb-export")


def read_csv(name):
    path = os.path.join(EXPORT_DIR, name)
    with open(path, encoding="cp1252", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    out = []
    for r in rows[1:]:
        r = r + [""] * (len(header) - len(r))
        out.append({h: (v.strip() if isinstance(v, str) else v) for h, v in zip(header, r) if h})
    return out


def num(v):
    if v in (None, ""):
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", ""))
    except ValueError:
        return 0.0


def clean_name(s):
    # QuickBooks Desktop appends "-®"/"-�" style markers to some names; keep the readable part.
    if not s:
        return ""
    s = str(s).strip()
    for junk in ("-®", "-�", "®", "�"):
        if s.endswith(junk):
            s = s[: -len(junk)].rstrip()
    return s


def import_lists(con):
    cur = con.cursor()
    # Accounts are upserted (not deleted) so entries made in the app keep pointing at the same ids.
    accounts = read_csv("QBX_Accounts.CSV")
    for a in accounts:
        full = a.get("Account", "")
        if not full:
            continue
        cur.execute(
            """INSERT INTO accounts(full_name, name, type, description, qb_balance, source) VALUES (?,?,?,?,?, 'qb')
               ON CONFLICT(full_name) DO UPDATE SET type=excluded.type, description=excluded.description,
               qb_balance=excluded.qb_balance""",
            (full, full.split(":")[-1], a.get("Type", ""), a.get("Description", ""), num(a.get("Balance Total"))),
        )
    print(f"  accounts: {len(accounts)}")

    cur.execute("DELETE FROM customers WHERE source='qb'")
    customers = read_csv("QBX_Customers.CSV")
    for c in customers:
        full = clean_name(c.get("Customer"))
        if not full:
            continue
        addr = ", ".join(x for x in (c.get(f"Bill to {i}") for i in range(1, 6)) if x)
        cur.execute(
            """INSERT OR IGNORE INTO customers(full_name, name, active, company, contact, phone, alt_phone, email,
               address, customer_type, terms, rep, qb_balance, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'qb')""",
            (full, full.split(":")[-1], c.get("Active Status") == "Active", clean_name(c.get("Company")),
             c.get("Primary Contact"), c.get("Main Phone"), c.get("Alt. Phone"), c.get("Main Email"), addr,
             c.get("Customer Type"), c.get("Terms"), c.get("Rep"), num(c.get("Balance Total"))),
        )
    print(f"  customers: {len(customers)}")

    cur.execute("DELETE FROM vendors WHERE source='qb'")
    vendors = read_csv("QBX_Vendors.CSV")
    for v in vendors:
        full = clean_name(v.get("Vendor"))
        if not full:
            continue
        addr = ", ".join(x for x in (v.get(f"Bill from {i}") for i in range(1, 6)) if x)
        cur.execute(
            """INSERT OR IGNORE INTO vendors(full_name, name, active, company, contact, phone, address, qb_balance, source)
               VALUES (?,?,?,?,?,?,?,?, 'qb')""",
            (full, full.split(":")[-1], v.get("Active Status") == "Active", clean_name(v.get("Company")),
             v.get("Primary Contact"), v.get("Main Phone"), addr, num(v.get("Balance Total"))),
        )
    print(f"  vendors: {len(vendors)}")

    cur.execute("DELETE FROM items WHERE source='qb'")
    items = read_csv("QBX_Items.CSV")
    for it in items:
        full = it.get("Item", "")
        if not full:
            continue
        cur.execute(
            """INSERT OR IGNORE INTO items(full_name, name, active, type, description, income_account, cogs_account,
               cost, price, qty_on_hand, unit, preferred_vendor, mpn, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?, 'qb')""",
            (full, full.split(":")[-1], it.get("Active Status") == "Active", it.get("Type"), it.get("Description"),
             it.get("Account"), it.get("COGS Account"), num(it.get("Cost")), num(it.get("Price")),
             num(it.get("Quantity On Hand")), it.get("U/M"), clean_name(it.get("Preferred Vendor")), it.get("MPN")),
        )
    print(f"  items: {len(items)}")


def account_resolver(con):
    """Journal rows name accounts by their last segment only; map them back to full chart paths."""
    by_full, by_leaf, type_of = {}, {}, {}
    for aid, full, name, typ in con.execute("SELECT id, full_name, name, type FROM accounts"):
        by_full[full] = aid
        type_of[aid] = typ
        by_leaf.setdefault(name, []).append(aid)
    ambiguous = set()

    def resolve(label):
        label = str(label).strip()
        if label in by_full:
            return by_full[label]
        hits = by_leaf.get(label)
        if hits:
            if len(set(hits)) > 1:
                ambiguous.add(label)
            return hits[0]
        # Not in the exported chart (QuickBooks leaves out inactive/hidden accounts such as "*Inventory Asset").
        # Keep it as its own account; borrow the type of a same-named account when there is one.
        twin = by_leaf.get(label.lstrip("*").strip())
        typ = type_of[twin[0]] if twin else "Unknown"
        cur = con.execute(
            "INSERT INTO accounts(full_name, name, type, description, source) VALUES (?,?,?, 'Seen in journal only', 'qb')",
            (label, label.split(":")[-1], typ),
        )
        by_full[label] = cur.lastrowid
        type_of[cur.lastrowid] = typ
        return cur.lastrowid

    return resolve, ambiguous


def find_journal():
    files = sorted(glob.glob(os.path.join(EXPORT_DIR, "*Journal*Full*.xlsx")) + glob.glob(os.path.join(EXPORT_DIR, "*Journal*.xlsx")))
    if not files:
        sys.exit("No journal .xlsx found in " + EXPORT_DIR)
    return files[0]


def import_journal(con):
    import openpyxl

    path = find_journal()
    print(f"  reading {os.path.basename(path)} (this takes about a minute)...")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb["Sheet1"] if "Sheet1" in wb.sheetnames else wb.worksheets[-1]

    rows = ws.iter_rows(values_only=True)
    header = next(rows)
    col = {str(h).strip(): i for i, h in enumerate(header) if h}
    need = ["Trans #", "Type", "Date", "Num", "Name", "Memo", "Account", "Debit", "Credit"]
    missing = [n for n in need if n not in col]
    if missing:
        sys.exit(f"Journal is missing columns: {missing}")

    cur = con.cursor()
    cur.execute("DELETE FROM splits WHERE trans_id IN (SELECT id FROM transactions WHERE source='qb')")
    cur.execute("DELETE FROM transactions WHERE source='qb'")
    resolve, ambiguous = account_resolver(con)

    trans_id = None
    n_tx = n_split = 0
    batch = []
    for r in rows:
        acct = r[col["Account"]]
        tno = r[col["Trans #"]]
        if tno not in (None, ""):
            d = r[col["Date"]]
            if isinstance(d, datetime):
                d = d.date().isoformat()
            elif d:
                d = datetime.strptime(str(d), "%d/%m/%Y").date().isoformat()
            cur.execute(
                "INSERT INTO transactions(qb_trans_no, type, date, num, name, source) VALUES (?,?,?,?,?, 'qb')",
                (int(tno), r[col["Type"]] or "", d, str(r[col["Num"]] or ""), clean_name(r[col["Name"]])),
            )
            trans_id = cur.lastrowid
            n_tx += 1
        if acct in (None, "") or trans_id is None:
            continue  # subtotal / blank / TOTAL rows
        batch.append((trans_id, resolve(acct), clean_name(r[col["Name"]]), str(r[col["Memo"]] or ""),
                      num(r[col["Debit"]]), num(r[col["Credit"]])))
        n_split += 1
        if len(batch) >= 5000:
            cur.executemany("INSERT INTO splits(trans_id, account_id, name, memo, debit, credit) VALUES (?,?,?,?,?,?)", batch)
            batch.clear()
    if batch:
        cur.executemany("INSERT INTO splits(trans_id, account_id, name, memo, debit, credit) VALUES (?,?,?,?,?,?)", batch)
    print(f"  transactions: {n_tx}, lines: {n_split}")
    if ambiguous:
        print(f"  note: {len(ambiguous)} account names matched more than one account (first match used): "
              + ", ".join(sorted(ambiguous)[:10]))
    unknown = con.execute("SELECT COUNT(*) FROM accounts WHERE type='Unknown'").fetchone()[0]
    if unknown:
        print(f"  note: {unknown} accounts appear in the journal but not in the chart of accounts (type 'Unknown')")


def read_detail_by_account(path):
    """Parse a QuickBooks 'Transaction Detail by Account' CSV into (trans_no, full_account, debit, credit)."""
    with open(path, encoding="cp1252", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    head = rows[0]
    ci = {h: i for i, h in enumerate(head) if h}
    stack, out = [], []
    for r in rows[1:]:
        if not r:
            continue
        first = r[0].strip()
        if first:
            if first.startswith("Total "):
                if stack and stack[-1] == first[6:]:
                    stack.pop()
                elif first[6:] in stack:
                    del stack[stack.index(first[6:]):]
            elif first != "TOTAL":
                stack.append(first)
            continue
        tno = r[ci["Trans #"]].strip()
        if tno and stack:
            out.append((int(tno), ":".join(stack), round(num(r[ci["Debit"]]), 2), round(num(r[ci["Credit"]]), 2)))
    return out


def fix_ambiguous_accounts(con):
    """Journal exports name accounts by their last segment only, so pairs such as
    'Accumulated Depreciation:Rental Assets' / 'Property, Plant & Equipment:Rental Assets' collide.
    Transaction Detail by Account exports (full names + Trans #) settle each line."""
    files = {"Fixed Asset": "QBX_TD_Fixed.CSV", "Other Current Liability": "QBX_TD_OCL.CSV"}
    detail, covered = defaultdict(list), set()
    for typ, fname in files.items():
        path = os.path.join(EXPORT_DIR, fname)
        if os.path.exists(path):
            covered.add(typ)
            for tno, full, dr, cr in read_detail_by_account(path):
                detail[(tno, full.split(":")[-1], dr, cr)].append(full)
    if not covered:
        print("  (no Transaction Detail exports found; clashing account names left as imported)")
        return
    acc = {r["full_name"]: (r["id"], r["type"]) for r in con.execute("SELECT id, full_name, type FROM accounts")}
    groups = defaultdict(list)
    for full, (aid, typ) in acc.items():
        groups[full.split(":")[-1]].append((full, aid, typ))
    moved = unresolved = 0
    for leaf, members in groups.items():
        if len(members) < 2:
            continue
        ids = [m[1] for m in members]
        fallback = [m for m in members if m[2] not in covered]
        lines = con.execute(
            f"""SELECT s.id, t.qb_trans_no, ROUND(s.debit,2), ROUND(s.credit,2) FROM splits s JOIN transactions t ON t.id=s.trans_id
                WHERE t.source='qb' AND s.account_id IN ({','.join('?' * len(ids))})""", ids).fetchall()
        for sid, tno, dr, cr in lines:
            hits = detail.get((tno, leaf, dr, cr))
            if hits:
                target = acc.get(hits.pop(0))
            elif len(fallback) == 1:
                target = (fallback[0][1], fallback[0][2])  # not in the covered reports -> the other account
            else:
                target = None
                unresolved += 1
            if target:
                con.execute("UPDATE splits SET account_id=? WHERE id=?", (target[0], sid))
                moved += 1
    print(f"  clashing account names: {moved} lines assigned to their full account, {unresolved} left unresolved")


def main():
    t = time.time()
    con = db.connect()
    db.init(con)
    print("Importing lists...")
    import_lists(con)
    print("Importing journal...")
    import_journal(con)
    fix_ambiguous_accounts(con)
    con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('imported_at', ?)", (datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    dr, cr = con.execute("SELECT ROUND(SUM(debit),2), ROUND(SUM(credit),2) FROM splits").fetchone()
    print(f"Total debits {dr:,.2f}  credits {cr:,.2f}  ({'balanced' if abs(dr - cr) < 0.05 else 'NOT balanced'})")
    print(f"Done in {time.time() - t:.0f}s -> {db.DB_PATH}")


if __name__ == "__main__":
    main()
