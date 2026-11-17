"""Smoke test on a COPY of books.db: logs in with a throwaway user and opens every page.

  python test_app.py
"""
import os
import re
import shutil
import sys
import tempfile

src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "books.db")
tmp = os.path.join(tempfile.mkdtemp(), "books.db")
shutil.copy(src, tmp)
os.environ["BOOKS_DB"] = tmp

import db  # noqa: E402
from werkzeug.security import generate_password_hash  # noqa: E402

con = db.connect(tmp)
pw = os.urandom(12).hex()
con.execute("INSERT INTO users(username, password_hash, role) VALUES ('smoketest', ?, 'admin')", (generate_password_hash(pw),))
con.commit()

from app import app  # noqa: E402

c = app.test_client()


def token(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


r = c.get("/login")
r = c.post("/login", data={"username": "smoketest", "password": pw, "csrf_token": token(r.text)})
assert r.status_code == 302, r.status_code

fails = 0
cust = con.execute("SELECT id FROM customers LIMIT 1").fetchone()[0]
vend = con.execute("SELECT id FROM vendors LIMIT 1").fetchone()[0]
bank = con.execute("SELECT id FROM accounts WHERE type='Bank' LIMIT 1").fetchone()[0]
tx = con.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0]
pages = ["/", "/accounts", f"/accounts/{bank}", f"/accounts/{bank}?page=1&start=2024-01-01", "/transactions",
         "/transactions?q=bank&type=Check", f"/transactions/{tx}", "/customers", "/customers?show=balance", "/vendors",
         f"/customers/{cust}", f"/vendors/{vend}", "/items", "/items?q=camera", "/reports", "/reports/profit-loss",
         "/reports/profit-loss?format=csv", "/reports/balance-sheet", "/reports/trial-balance", "/reports/aging/customers",
         "/reports/aging/vendors", "/new/journal", "/new/invoice", "/new/bill", "/new/payment", "/new/billpay",
         "/new/expense", "/admin/users", "/password", "/search?q=bank", "/search?q=35000", "/search"]
for p in pages:
    r = c.get(p)
    ok = r.status_code == 200
    fails += not ok
    print(("OK  " if ok else "FAIL"), r.status_code, p)

# post a balanced journal entry and an unbalanced one
html = c.get("/new/journal").text
acc = con.execute("SELECT full_name FROM accounts WHERE type='Expense' LIMIT 1").fetchone()[0]
bank_name = con.execute("SELECT full_name FROM accounts WHERE id=?", (bank,)).fetchone()[0]
data = {"csrf_token": token(html), "date": "2026-11-16", "num": "T1", "memo": "smoke",
        "account": [acc, bank_name], "debit": ["100", ""], "credit": ["", "100"], "lname": ["", ""], "lmemo": ["", ""]}
r = c.post("/new/journal", data=data)
print("journal balanced ->", r.status_code, r.headers.get("Location"))
fails += r.status_code != 302
data["credit"] = ["", "90"]
r = c.post("/new/journal", data=data)
print("journal unbalanced rejected ->", "Debits" in r.text)
fails += "Debits" not in r.text
r = c.post("/new/journal", data={**data, "csrf_token": "bad"})
print("bad csrf rejected ->", r.status_code == 400)
fails += r.status_code != 400

# add an item, then edit it
r = c.get("/items/new")
print("new item form ->", r.status_code); fails += r.status_code != 200
item_data = {"csrf_token": token(r.text), "type": "Service", "name": "SMOKE-ITEM-1", "parent": "", "mpn": "",
             "description": "Smoke test item", "price": "1,500", "cost": "900",
             "income_account": con.execute("SELECT full_name FROM accounts WHERE type='Income' LIMIT 1").fetchone()[0],
             "cogs_account": "",
             "unit": "", "preferred_vendor": "", "active": "1"}
r = c.post("/items/new", data=item_data)
row = con.execute("SELECT id, price FROM items WHERE full_name='SMOKE-ITEM-1'").fetchone()
print("item added ->", r.status_code == 302 and row is not None and row[1] == 1500); fails += not (row and row[1] == 1500)
r = c.get(f"/items/{row[0]}/edit")
r = c.post(f"/items/{row[0]}/edit", data={**item_data, "csrf_token": token(r.text), "price": "1750"})
print("item edited ->", con.execute("SELECT price FROM items WHERE id=?", (row[0],)).fetchone()[0] == 1750)
r = c.post("/items/new", data={**item_data, "csrf_token": token(c.get('/items/new').text)})
print("duplicate rejected ->", "already exists" in r.text); fails += "already exists" not in r.text
r = c.post("/items/new", data={**item_data, "name": "SMOKE-2", "income_account": "No Such Account",
                                "csrf_token": token(c.get('/items/new').text)})
print("bad account rejected ->", "in the chart of accounts" in r.text); fails += "in the chart of accounts" not in r.text

# balance sheet must balance
html = c.get("/reports/balance-sheet").text
print("balance sheet balanced ->", "differ by" not in html)

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
