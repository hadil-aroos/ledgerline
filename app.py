"""Ledgerline - a small multi-user bookkeeping web app over books.db.

Run:  python app.py            (development, http://127.0.0.1:8080)
      python serve.py          (production server for your team, see README)
"""
import csv
import io
import math
import os
import secrets
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template, request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)


def _secret_key():
    path = os.path.join(BASE, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


app.config.update(
    SECRET_KEY=_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("BOOKS_HTTPS") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=10),
)
def _load_env_file():
    """Read KEY=VALUE lines from .env next to the app (kept out of git) into the environment."""
    path = os.path.join(BASE, ".env")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())


_load_env_file()
COMPANY = os.environ.get("BOOKS_COMPANY", "My Company")
CURRENCY = os.environ.get("BOOKS_CURRENCY", "USD")
FY_START_MONTH = int(os.environ.get("BOOKS_FY_START_MONTH", "1"))  # 1 = January
PAGE = 100

ROLES = {"viewer": 0, "accountant": 1, "admin": 2}


# ---------------------------------------------------------------- helpers

def get_db():
    if "db" not in g:
        g.db = db.connect()
    return g.db


@app.teardown_appcontext
def _close(_exc):
    con = g.pop("db", None)
    if con is not None:
        con.close()


def audit(action, detail=""):
    get_db().execute("INSERT INTO audit_log(username, action, detail) VALUES (?,?,?)",
                     (session.get("username"), action, detail))


@app.template_filter("money")
def money(v, blank_zero=False):
    if v is None:
        return ""
    v = round(float(v), 2)
    if blank_zero and abs(v) < 0.005:
        return ""
    s = f"{abs(v):,.2f}"
    return f"-{s}" if v < 0 else s


@app.template_filter("dmy")
def dmy(iso):
    try:
        return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return iso or ""


def parse_date(s, default=None):
    if not s:
        return default
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    return default


def fiscal_year_bounds(today=None):
    today = today or date.today()
    y = today.year if today.month >= FY_START_MONTH else today.year - 1
    start = date(y, FY_START_MONTH, 1)
    end = date(y + 1, FY_START_MONTH, 1) - timedelta(days=1)
    return start, end


def natural_sign(acc_type):
    return -1 if acc_type in db.CREDIT_NORMAL else 1


@app.context_processor
def _globals():
    return dict(company=COMPANY, currency=CURRENCY, me=current_user(), csrf_token=csrf_token, ROLES=ROLES)


# ---------------------------------------------------------------- auth

def current_user():
    uid = session.get("uid")
    if not uid:
        return None
    if "user" not in g:
        g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()
    return g.user


def login_required(role="viewer"):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            u = current_user()
            if not u:
                return redirect(url_for("login", next=request.full_path))
            if ROLES[u["role"]] < ROLES[role]:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


def csrf_token():
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(32)
    return session["csrf"]


@app.before_request
def _csrf_check():
    if request.method == "POST":
        sent = request.form.get("csrf_token", "")
        if not sent or not secrets.compare_digest(sent, session.get("csrf", "")):
            abort(400, "Form expired - go back, refresh the page and try again.")


_FAILED = defaultdict(list)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        ip = request.remote_addr or "?"
        recent = [t for t in _FAILED[ip] if t > time.time() - 900]
        _FAILED[ip] = recent
        if len(recent) >= 10:
            flash("Too many failed attempts. Wait 15 minutes and try again.", "error")
            return render_template("login.html"), 429
        u = get_db().execute("SELECT * FROM users WHERE username=? AND active=1",
                             (request.form.get("username", "").strip(),)).fetchone()
        if u and check_password_hash(u["password_hash"], request.form.get("password", "")):
            session.clear()
            session.permanent = True
            session["uid"] = u["id"]
            session["username"] = u["username"]
            get_db().execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?", (u["id"],))
            audit("login")
            get_db().commit()
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("dashboard"))
        _FAILED[ip].append(time.time())
        flash("Wrong username or password.", "error")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/password", methods=["GET", "POST"])
@login_required()
def change_password():
    if request.method == "POST":
        u = current_user()
        new = request.form.get("new", "")
        if not check_password_hash(u["password_hash"], request.form.get("current", "")):
            flash("Current password is wrong.", "error")
        elif len(new) < 8:
            flash("New password must be at least 8 characters.", "error")
        elif new != request.form.get("confirm"):
            flash("The two new passwords don't match.", "error")
        else:
            get_db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(new), u["id"]))
            audit("change password")
            get_db().commit()
            flash("Password changed.", "ok")
            return redirect(url_for("dashboard"))
    return render_template("password.html")


# ---------------------------------------------------------------- balances

def account_balances(start=None, end=None, types=None):
    """{account_id: debit-credit} for splits dated within [start, end]."""
    q = ["SELECT s.account_id, SUM(s.debit - s.credit) FROM splits s JOIN transactions t ON t.id=s.trans_id"]
    where, args = [], []
    if start:
        where.append("t.date >= ?"); args.append(start.isoformat())
    if end:
        where.append("t.date <= ?"); args.append(end.isoformat())
    if types:
        where.append(f"s.account_id IN (SELECT id FROM accounts WHERE type IN ({','.join('?' * len(types))}))")
        args += list(types)
    if where:
        q.append("WHERE " + " AND ".join(where))
    q.append("GROUP BY s.account_id")
    return {r[0]: r[1] or 0.0 for r in get_db().execute(" ".join(q), args)}


def account_tree(bal, types):
    """Rows of the chart for the given types, with parent totals rolled up. Amounts in natural sign."""
    rows = get_db().execute(
        f"SELECT id, full_name, name, type FROM accounts WHERE type IN ({','.join('?' * len(types))}) ORDER BY full_name",
        types).fetchall()
    total_by_path = defaultdict(float)
    for r in rows:
        v = bal.get(r["id"], 0.0) * natural_sign(r["type"])
        parts = r["full_name"].split(":")
        for i in range(1, len(parts) + 1):
            total_by_path[":".join(parts[:i])] += v
    known = {r["full_name"] for r in rows}
    out = []
    for r in rows:
        own = bal.get(r["id"], 0.0) * natural_sign(r["type"])
        total = total_by_path[r["full_name"]]
        has_children = any(k.startswith(r["full_name"] + ":") for k in known)
        out.append(dict(id=r["id"], full_name=r["full_name"], name=r["name"], type=r["type"],
                        depth=r["full_name"].count(":"), own=own, total=total, parent=has_children))
    return out


def sum_types(bal, types):
    ids = {r[0]: r[1] for r in get_db().execute(
        f"SELECT id, type FROM accounts WHERE type IN ({','.join('?' * len(types))})", types)}
    return sum(v * natural_sign(ids[a]) for a, v in bal.items() if a in ids)


# ---------------------------------------------------------------- pages

@app.route("/")
@login_required()
def dashboard():
    con = get_db()
    fy_start, fy_end = fiscal_year_bounds()
    today = date.today()
    bal_all = account_balances(end=today)
    fy = account_balances(fy_start, min(fy_end, today))
    income = sum_types(fy, db.INCOME_TYPES)
    cogs = sum_types(fy, db.COGS_TYPES)
    expenses = sum_types(fy, db.EXPENSE_TYPES)
    banks = [dict(id=r["id"], name=r["name"], bal=bal_all.get(r["id"], 0.0))
             for r in con.execute("SELECT id, name FROM accounts WHERE type='Bank' AND active=1 ORDER BY name")]
    banks = [b for b in banks if abs(b["bal"]) > 0.005]

    # last 12 months income vs expense
    months = []
    m0 = date(today.year, today.month, 1)
    for i in range(11, -1, -1):
        y, m = m0.year, m0.month - i
        while m <= 0:
            m += 12; y -= 1
        start = date(y, m, 1)
        end = (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))
        months.append((start, end))
    rows = con.execute(
        """SELECT substr(t.date,1,7) ym, a.type, SUM(s.debit-s.credit) v FROM splits s
           JOIN transactions t ON t.id=s.trans_id JOIN accounts a ON a.id=s.account_id
           WHERE t.date BETWEEN ? AND ? AND a.type IN ('Income','Other Income','Cost of Goods Sold','Expense','Other Expense')
           GROUP BY ym, a.type""", (months[0][0].isoformat(), months[-1][1].isoformat())).fetchall()
    per = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        if r["type"] in db.INCOME_TYPES:
            per[r["ym"]][0] += -r["v"]
        else:
            per[r["ym"]][1] += r["v"]
    chart = [dict(label=s.strftime("%b %y"), income=per[s.strftime("%Y-%m")][0], expense=per[s.strftime("%Y-%m")][1])
             for s, _ in months]
    peak = max([1.0] + [max(c["income"], c["expense"]) for c in chart])

    ar = sum_types(bal_all, ("Accounts Receivable",))
    ap = sum_types(bal_all, ("Accounts Payable",))
    recent = con.execute("SELECT t.*, (SELECT SUM(debit) FROM splits WHERE trans_id=t.id) amount FROM transactions t "
                         "WHERE date <= ? ORDER BY date DESC, id DESC LIMIT 12",
                         (today.isoformat(),)).fetchall()
    imported = con.execute("SELECT value FROM settings WHERE key='imported_at'").fetchone()
    return render_template("dashboard.html", income=income, cogs=cogs, expenses=expenses,
                           net=income - cogs - expenses, banks=banks, cash=sum(b["bal"] for b in banks),
                           ar=ar, ap=ap, chart=chart, peak=peak, recent=recent, fy_start=fy_start,
                           fy_end=fy_end, imported=imported[0] if imported else None)


@app.route("/accounts")
@login_required()
def accounts():
    bal = account_balances(end=parse_date(request.args.get("asof"), date.today()))
    groups = [("Assets", db.ASSET_TYPES), ("Liabilities", db.LIABILITY_TYPES), ("Equity", db.EQUITY_TYPES),
              ("Income", db.INCOME_TYPES), ("Cost of Goods Sold", db.COGS_TYPES), ("Expenses", db.EXPENSE_TYPES),
              ("Other", ("Non-Posting", "Unknown"))]
    q = request.args.get("q", "").strip().lower()
    data = []
    for title, types in groups:
        rows = account_tree(bal, types)
        if q:
            rows = [r for r in rows if q in r["full_name"].lower()]
        if rows:
            data.append((title, rows))
    return render_template("accounts.html", groups=data, q=q, asof=request.args.get("asof", ""))


@app.route("/accounts/<int:aid>")
@login_required()
def register(aid):
    con = get_db()
    acc = con.execute("SELECT * FROM accounts WHERE id=?", (aid,)).fetchone() or abort(404)
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    include_sub = request.args.get("sub", "1") == "1"
    ids = [aid]
    if include_sub:
        ids += [r[0] for r in con.execute("SELECT id FROM accounts WHERE full_name LIKE ?", (acc["full_name"] + ":%",))]
    marks = ",".join("?" * len(ids))
    sign = natural_sign(acc["type"])
    opening = 0.0
    if start:
        opening = con.execute(
            f"SELECT COALESCE(SUM(s.debit-s.credit),0) FROM splits s JOIN transactions t ON t.id=s.trans_id "
            f"WHERE s.account_id IN ({marks}) AND t.date < ?", ids + [start.isoformat()]).fetchone()[0] * sign
    where = [f"s.account_id IN ({marks})"]
    args = list(ids)
    if start:
        where.append("t.date >= ?"); args.append(start.isoformat())
    if end:
        where.append("t.date <= ?"); args.append(end.isoformat())
    total = con.execute(f"SELECT COUNT(*) FROM splits s JOIN transactions t ON t.id=s.trans_id WHERE {' AND '.join(where)}",
                        args).fetchone()[0]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, request.args.get("page", pages, type=int)), pages)  # default: last page (most recent)
    rows = con.execute(
        f"""SELECT * FROM (
              SELECT t.id tid, t.date, t.type, t.num, s.name, s.memo, s.debit, s.credit, a.name acct,
                     SUM(s.debit - s.credit) OVER (ORDER BY t.date, t.id, s.id) * ? + ? AS running
              FROM splits s JOIN transactions t ON t.id=s.trans_id JOIN accounts a ON a.id=s.account_id
              WHERE {' AND '.join(where)}
            ) ORDER BY date, tid LIMIT ? OFFSET ?""",
        [sign, opening] + args + [PAGE, (page - 1) * PAGE]).fetchall()
    return render_template("register.html", acc=acc, rows=rows, page=page, pages=pages, total=total,
                           opening=opening, start=request.args.get("start", ""), end=request.args.get("end", ""),
                           sub=include_sub)


def _tx_filters():
    where, args = [], []
    q = request.args.get("q", "").strip()
    if q:
        where.append("(t.name LIKE ? OR t.num LIKE ? OR t.memo LIKE ? OR t.id IN "
                     "(SELECT trans_id FROM splits WHERE memo LIKE ? OR name LIKE ?))")
        args += [f"%{q}%"] * 5
    if request.args.get("type"):
        where.append("t.type = ?"); args.append(request.args["type"])
    s, e = parse_date(request.args.get("start")), parse_date(request.args.get("end"))
    if s:
        where.append("t.date >= ?"); args.append(s.isoformat())
    if e:
        where.append("t.date <= ?"); args.append(e.isoformat())
    return ("WHERE " + " AND ".join(where)) if where else "", args


@app.route("/transactions")
@login_required()
def transactions():
    con = get_db()
    where, args = _tx_filters()
    total = con.execute(f"SELECT COUNT(*) FROM transactions t {where}", args).fetchone()[0]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = con.execute(
        f"""SELECT t.*, (SELECT SUM(debit) FROM splits WHERE trans_id=t.id) amount
            FROM transactions t {where} ORDER BY t.date DESC, t.id DESC LIMIT ? OFFSET ?""",
        args + [PAGE, (page - 1) * PAGE]).fetchall()
    types = [r[0] for r in con.execute("SELECT DISTINCT type FROM transactions ORDER BY type")]
    return render_template("transactions.html", rows=rows, total=total, page=page, pages=pages, types=types,
                           args=request.args)


@app.route("/transactions/<int:tid>")
@login_required()
def transaction(tid):
    con = get_db()
    t = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone() or abort(404)
    splits = con.execute("""SELECT s.*, a.full_name acct, a.id aid FROM splits s JOIN accounts a ON a.id=s.account_id
                            WHERE s.trans_id=? ORDER BY s.id""", (tid,)).fetchall()
    return render_template("transaction.html", t=t, splits=splits)


@app.route("/transactions/<int:tid>/delete", methods=["POST"])
@login_required("admin")
def delete_transaction(tid):
    con = get_db()
    t = con.execute("SELECT * FROM transactions WHERE id=?", (tid,)).fetchone() or abort(404)
    if t["source"] != "app":
        flash("Imported QuickBooks history can't be deleted here. Post a correcting journal entry instead.", "error")
        return redirect(url_for("transaction", tid=tid))
    con.execute("DELETE FROM splits WHERE trans_id=?", (tid,))
    con.execute("DELETE FROM transactions WHERE id=?", (tid,))
    audit("delete transaction", f"#{tid} {t['type']} {t['date']} {t['name']}")
    con.commit()
    flash("Transaction deleted.", "ok")
    return redirect(url_for("transactions"))


def _party_balances(kind):
    acc_type = "Accounts Receivable" if kind == "customers" else "Accounts Payable"
    sign = 1 if kind == "customers" else -1
    return {r[0]: r[1] * sign for r in get_db().execute(
        """SELECT s.name, SUM(s.debit-s.credit) FROM splits s JOIN accounts a ON a.id=s.account_id
           WHERE a.type=? GROUP BY s.name""", (acc_type,))}


def _party_list(kind):
    con = get_db()
    q = request.args.get("q", "").strip()
    show = request.args.get("show", "active")
    bal = _party_balances(kind)
    where, args = [], []
    if q:
        where.append("(full_name LIKE ? OR company LIKE ? OR phone LIKE ? OR contact LIKE ?)")
        args += [f"%{q}%"] * 4
    if show == "active":
        where.append("active=1")
    rows = con.execute(f"SELECT * FROM {kind} {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY full_name",
                       args).fetchall()
    rows = [dict(r, balance=bal.get(r["full_name"], 0.0)) for r in rows]
    if show == "balance":
        rows = [r for r in rows if abs(r["balance"]) > 0.005]
    total = sum(r["balance"] for r in rows)
    return render_template("parties.html", kind=kind, rows=rows, q=q, show=show, total=total)


@app.route("/customers")
@login_required()
def customers():
    return _party_list("customers")


@app.route("/vendors")
@login_required()
def vendors():
    return _party_list("vendors")


@app.route("/<kind>/<int:pid>")
@login_required()
def party(kind, pid):
    if kind not in ("customers", "vendors"):
        abort(404)
    con = get_db()
    p = con.execute(f"SELECT * FROM {kind} WHERE id=?", (pid,)).fetchone() or abort(404)
    names = [p["full_name"]]
    rows = con.execute(
        """SELECT t.*, (SELECT SUM(debit) FROM splits WHERE trans_id=t.id) amount FROM transactions t
           WHERE t.name = ? OR t.name LIKE ? ORDER BY t.date DESC, t.id DESC LIMIT 500""",
        (p["full_name"], p["full_name"] + ":%")).fetchall()
    bal = _party_balances(kind)
    balance = sum(v for k, v in bal.items() if k == p["full_name"] or (k or "").startswith(p["full_name"] + ":"))
    return render_template("party.html", kind=kind, p=p, rows=rows, balance=balance)


@app.route("/items")
@login_required()
def items():
    con = get_db()
    q = request.args.get("q", "").strip()
    where, args = ["active=1"] if request.args.get("all") != "1" else [], []
    if q:
        where.append("(full_name LIKE ? OR description LIKE ? OR mpn LIKE ?)")
        args += [f"%{q}%"] * 3
    total = con.execute(f"SELECT COUNT(*) FROM items {'WHERE ' + ' AND '.join(where) if where else ''}", args).fetchone()[0]
    pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, request.args.get("page", 1, type=int)), pages)
    rows = con.execute(f"SELECT * FROM items {'WHERE ' + ' AND '.join(where) if where else ''} ORDER BY full_name LIMIT ? OFFSET ?",
                       args + [PAGE, (page - 1) * PAGE]).fetchall()
    return render_template("items.html", rows=rows, q=q, page=page, pages=pages, total=total,
                           all=request.args.get("all") == "1")


ITEM_TYPES = ["Service", "Inventory Part", "Non-inventory Part", "Other Charge"]


@app.route("/items/new", methods=["GET", "POST"])
@app.route("/items/<int:iid>/edit", methods=["GET", "POST"])
@login_required("accountant")
def edit_item(iid=None):
    con = get_db()
    item = con.execute("SELECT * FROM items WHERE id=?", (iid,)).fetchone() if iid else None
    if iid and not item:
        abort(404)
    if request.method == "POST":
        f = request.form
        parent, name = f.get("parent", "").strip().strip(":"), f.get("name", "").strip()
        full = f"{parent}:{name}" if parent else name
        income, cogs = f.get("income_account", "").strip(), f.get("cogs_account", "").strip()
        errors = []
        if not name or ":" in name:
            errors.append("Item name is required and can't contain ':' (use the 'Sub-item of' box for groups).")
        if f.get("type") not in ITEM_TYPES:
            errors.append("Choose an item type.")
        for label, acc in (("Income account", income), ("Cost account", cogs)):
            if acc and not con.execute("SELECT 1 FROM accounts WHERE full_name=?", (acc,)).fetchone():
                errors.append(f"{label} '{acc}' isn't in the chart of accounts.")
        if not income:
            errors.append("Income account is required (where sales of this item are posted).")
        clash = con.execute("SELECT id FROM items WHERE full_name=?", (full,)).fetchone()
        if clash and clash[0] != iid:
            errors.append(f"An item called '{full}' already exists.")
        if errors:
            for e in errors:
                flash(e, "error")
        else:
            vals = (full, name, 1 if f.get("active") else 0, f["type"], f.get("description", "").strip(), income, cogs,
                    _amount(f.get("cost") or 0), _amount(f.get("price") or 0), f.get("unit", "").strip(),
                    f.get("preferred_vendor", "").strip(), f.get("mpn", "").strip())
            if item:
                # source='app' so a later QuickBooks re-import doesn't overwrite these edits
                con.execute("""UPDATE items SET full_name=?, name=?, active=?, type=?, description=?, income_account=?,
                               cogs_account=?, cost=?, price=?, unit=?, preferred_vendor=?, mpn=?, source='app' WHERE id=?""",
                            vals + (iid,))
                audit("edit item", full)
                flash(f"Saved {full}.", "ok")
            else:
                con.execute("""INSERT INTO items(full_name, name, active, type, description, income_account, cogs_account,
                               cost, price, unit, preferred_vendor, mpn, source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'app')""", vals)
                audit("add item", full)
                flash(f"Added {full}.", "ok")
            con.commit()
            return redirect(url_for("items", q=name))
    parents = [r[0] for r in con.execute(
        "SELECT DISTINCT substr(full_name, 1, instr(full_name, ':') - 1) FROM items WHERE instr(full_name, ':') > 0 ORDER BY 1")]
    accounts = _account_options(db.INCOME_TYPES + db.COGS_TYPES + db.EXPENSE_TYPES + ("Other Current Asset",))
    vendors = [r[0] for r in con.execute("SELECT full_name FROM vendors WHERE active=1 ORDER BY full_name")]
    if request.method == "POST":
        d = request.form.to_dict()
    elif item:
        d = dict(item)
        d["parent"] = item["full_name"].rsplit(":", 1)[0] if ":" in item["full_name"] else ""
        d["active"] = "1" if item["active"] else ""
        for k in ("price", "cost"):
            d[k] = f"{d[k]:.2f}" if d[k] else ""
    else:
        d = {"type": "Service", "active": "1",
             "income_account": os.environ.get("BOOKS_DEFAULT_INCOME_ACCOUNT", ""),
             "cogs_account": os.environ.get("BOOKS_DEFAULT_COGS_ACCOUNT", "")}
    return render_template("item_form.html", item=item, d=d, parents=parents, accounts=accounts, vendors=vendors,
                           types=ITEM_TYPES)


@app.route("/search")
@login_required()
def search():
    con = get_db()
    q = request.args.get("q", "").strip()
    res = dict(customers=[], vendors=[], products=[], accounts=[], transactions=[])
    if q:
        like = f"%{q}%"
        res["customers"] = con.execute("SELECT id, full_name, phone FROM customers WHERE full_name LIKE ? OR company LIKE ? "
                                       "OR phone LIKE ? ORDER BY active DESC, full_name LIMIT 15", (like, like, like)).fetchall()
        res["vendors"] = con.execute("SELECT id, full_name, phone FROM vendors WHERE full_name LIKE ? OR company LIKE ? "
                                     "OR phone LIKE ? ORDER BY active DESC, full_name LIMIT 15", (like, like, like)).fetchall()
        res["products"] = con.execute("SELECT full_name, description, price FROM items WHERE full_name LIKE ? OR description LIKE ? "
                                   "OR mpn LIKE ? ORDER BY active DESC, full_name LIMIT 15", (like, like, like)).fetchall()
        res["accounts"] = con.execute("SELECT id, full_name, type FROM accounts WHERE full_name LIKE ? ORDER BY full_name LIMIT 10",
                                      (like,)).fetchall()
        amount = _amount(q) if any(ch.isdigit() for ch in q) else 0
        res["transactions"] = con.execute(
            """SELECT t.*, (SELECT SUM(debit) FROM splits WHERE trans_id=t.id) amount FROM transactions t
               WHERE t.num = ? OR t.num LIKE ? OR CAST(t.qb_trans_no AS TEXT) = ?
                  OR (? > 0 AND t.id IN (SELECT trans_id FROM splits WHERE ROUND(debit,2)=? OR ROUND(credit,2)=?))
               ORDER BY t.date DESC LIMIT 25""", (q, like, q, amount, amount, amount)).fetchall()
    return render_template("search.html", q=q, res=res)


# ---------------------------------------------------------------- reports

def _period():
    fy_start, fy_end = fiscal_year_bounds()
    start = parse_date(request.args.get("start"), fy_start)
    end = parse_date(request.args.get("end"), min(fy_end, date.today()))
    return start, end


def _csv(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue().encode("utf-8-sig"), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.route("/reports")
@login_required()
def reports():
    return render_template("reports.html")


@app.route("/reports/profit-loss")
@login_required()
def profit_loss():
    start, end = _period()
    bal = account_balances(start, end)
    sections = []
    for title, types in (("Income", db.INCOME_TYPES), ("Cost of Goods Sold", db.COGS_TYPES), ("Expenses", db.EXPENSE_TYPES)):
        rows = [r for r in account_tree(bal, types) if abs(r["total"]) > 0.005]
        sections.append((title, rows, sum_types(bal, types)))
    income, cogs, exp = (s[2] for s in sections)
    if request.args.get("format") == "csv":
        out = []
        for title, rows, tot in sections:
            out += [[r["full_name"], r["type"], round(r["total"], 2)] for r in rows if not r["parent"] or abs(r["own"]) > 0.005]
            out.append([f"Total {title}", "", round(tot, 2)])
        out.append(["Net profit", "", round(income - cogs - exp, 2)])
        return _csv(f"profit-loss_{start}_{end}.csv", ["Account", "Type", "Amount"], out)
    return render_template("pl.html", sections=sections, income=income, cogs=cogs, expenses=exp,
                           gross=income - cogs, net=income - cogs - exp, start=start, end=end)


@app.route("/reports/balance-sheet")
@login_required()
def balance_sheet():
    asof = parse_date(request.args.get("asof"), date.today())
    bal = account_balances(end=asof)
    fy_start, _ = fiscal_year_bounds(asof)
    # Profit before this fiscal year rolls into retained earnings; this year's shows as net income.
    pl_types = db.INCOME_TYPES + db.COGS_TYPES + db.EXPENSE_TYPES
    before = account_balances(end=fy_start - timedelta(days=1), types=pl_types)
    this_year = account_balances(fy_start, asof, types=pl_types)
    retained = -sum(before.values())
    net_income = -sum(this_year.values())
    assets = [r for r in account_tree(bal, db.ASSET_TYPES) if abs(r["total"]) > 0.005]
    liabs = [r for r in account_tree(bal, db.LIABILITY_TYPES) if abs(r["total"]) > 0.005]
    equity = [r for r in account_tree(bal, db.EQUITY_TYPES) if abs(r["total"]) > 0.005]
    ta = sum_types(bal, db.ASSET_TYPES)
    tl = sum_types(bal, db.LIABILITY_TYPES)
    te = sum_types(bal, db.EQUITY_TYPES) + retained + net_income
    other = sum(v for a, v in bal.items()) - 0  # all splits net to zero when the books balance
    if request.args.get("format") == "csv":
        out = [[r["full_name"], round(r["total"], 2)] for r in assets if not r["parent"] or abs(r["own"]) > 0.005]
        out.append(["TOTAL ASSETS", round(ta, 2)])
        out += [[r["full_name"], round(r["total"], 2)] for r in liabs if not r["parent"] or abs(r["own"]) > 0.005]
        out.append(["TOTAL LIABILITIES", round(tl, 2)])
        out += [[r["full_name"], round(r["total"], 2)] for r in equity if not r["parent"] or abs(r["own"]) > 0.005]
        out += [["Retained Earnings (prior years)", round(retained, 2)], ["Net Income (this year)", round(net_income, 2)],
                ["TOTAL EQUITY", round(te, 2)], ["TOTAL LIABILITIES & EQUITY", round(tl + te, 2)]]
        return _csv(f"balance-sheet_{asof}.csv", ["Account", "Amount"], out)
    return render_template("bs.html", asof=asof, assets=assets, liabs=liabs, equity=equity, ta=ta, tl=tl, te=te,
                           retained=retained, net_income=net_income, diff=round(other, 2))


@app.route("/reports/trial-balance")
@login_required()
def trial_balance():
    asof = parse_date(request.args.get("asof"), date.today())
    bal = account_balances(end=asof)
    rows = get_db().execute("SELECT id, full_name, type FROM accounts ORDER BY full_name").fetchall()
    lines = [(r["full_name"], r["type"], max(bal.get(r["id"], 0), 0), max(-bal.get(r["id"], 0), 0))
             for r in rows if abs(bal.get(r["id"], 0)) > 0.005]
    td, tc = sum(l[2] for l in lines), sum(l[3] for l in lines)
    if request.args.get("format") == "csv":
        return _csv(f"trial-balance_{asof}.csv", ["Account", "Type", "Debit", "Credit"],
                    [[a, t, round(d, 2), round(c, 2)] for a, t, d, c in lines] + [["TOTAL", "", round(td, 2), round(tc, 2)]])
    return render_template("tb.html", lines=lines, td=td, tc=tc, asof=asof)


@app.route("/reports/aging/<kind>")
@login_required()
def aging(kind):
    """Open balance per customer/vendor, split by the age of the transactions that make it up (FIFO)."""
    if kind not in ("customers", "vendors"):
        abort(404)
    asof = parse_date(request.args.get("asof"), date.today())
    acc_type = "Accounts Receivable" if kind == "customers" else "Accounts Payable"
    sign = 1 if kind == "customers" else -1
    rows = get_db().execute(
        """SELECT s.name, t.date, SUM(s.debit-s.credit) v FROM splits s JOIN transactions t ON t.id=s.trans_id
           JOIN accounts a ON a.id=s.account_id WHERE a.type=? AND t.date<=? AND s.name<>''
           GROUP BY s.name, t.id ORDER BY s.name, t.date""", (acc_type, asof.isoformat())).fetchall()
    per = defaultdict(list)
    for r in rows:
        per[r["name"]].append((r["date"], r["v"] * sign))
    buckets = ["Current", "1-30", "31-60", "61-90", "Over 90"]
    out = []
    for name, entries in per.items():
        charges = [[d, v] for d, v in entries if v > 0]
        credit = -sum(v for _, v in entries if v < 0)
        for c in charges:  # apply payments to oldest charges first
            used = min(c[1], credit); c[1] -= used; credit -= used
        b = [0.0] * 5
        for d, v in charges:
            if v <= 0.005:
                continue
            days = (asof - date.fromisoformat(d)).days
            b[0 if days <= 0 else 1 if days <= 30 else 2 if days <= 60 else 3 if days <= 90 else 4] += v
        if credit > 0.005:
            b[0] -= credit
        if abs(sum(b)) > 0.005:
            out.append((name, b, sum(b)))
    out.sort(key=lambda x: -x[2])
    totals = [sum(o[1][i] for o in out) for i in range(5)]
    if request.args.get("format") == "csv":
        return _csv(f"{kind}-aging_{asof}.csv", ["Name"] + buckets + ["Total"],
                    [[n] + [round(x, 2) for x in b] + [round(t, 2)] for n, b, t in out])
    return render_template("aging.html", kind=kind, rows=out, buckets=buckets, totals=totals, asof=asof)


# ---------------------------------------------------------------- entry forms

def _account_options(types=None):
    con = get_db()
    if types:
        return con.execute(f"SELECT id, full_name, type FROM accounts WHERE active=1 AND type IN ({','.join('?' * len(types))}) "
                           "ORDER BY full_name", types).fetchall()
    return con.execute("SELECT id, full_name, type FROM accounts WHERE active=1 AND type NOT IN ('Non-Posting') "
                       "ORDER BY full_name").fetchall()


def _account_id(full_name_or_id):
    con = get_db()
    r = None
    if str(full_name_or_id).isdigit():
        r = con.execute("SELECT id FROM accounts WHERE id=?", (int(full_name_or_id),)).fetchone()
    if not r:
        r = con.execute("SELECT id FROM accounts WHERE full_name=?", (full_name_or_id,)).fetchone()
    return r[0] if r else None


def _first_account(acc_type):
    r = get_db().execute("SELECT id FROM accounts WHERE type=? AND active=1 ORDER BY id LIMIT 1", (acc_type,)).fetchone()
    return r[0] if r else None


def _amount(s):
    try:
        return round(float(str(s).replace(",", "")), 2)
    except ValueError:
        return 0.0


def _save(ttype, tdate, num, name, memo, lines):
    """lines: [(account_id, name, memo, debit, credit)] - must balance."""
    dr = round(sum(l[3] for l in lines), 2)
    cr = round(sum(l[4] for l in lines), 2)
    if not lines or dr <= 0 or abs(dr - cr) > 0.004:
        raise ValueError(f"Debits ({dr:,.2f}) and credits ({cr:,.2f}) must be equal and above zero.")
    if any(l[0] is None for l in lines):
        raise ValueError("Every line needs an account.")
    con = get_db()
    cur = con.execute("INSERT INTO transactions(type, date, num, name, memo, source, created_by) VALUES (?,?,?,?,?, 'app', ?)",
                      (ttype, tdate.isoformat(), num, name, memo, session.get("username")))
    tid = cur.lastrowid
    con.executemany("INSERT INTO splits(trans_id, account_id, name, memo, debit, credit) VALUES (?,?,?,?,?,?)",
                    [(tid,) + tuple(l) for l in lines])
    audit("create " + ttype, f"#{tid} {tdate} {name} {dr:,.2f}")
    con.commit()
    return tid


@app.route("/new/journal", methods=["GET", "POST"])
@login_required("accountant")
def new_journal():
    if request.method == "POST":
        f = request.form
        lines = []
        for acc, nm, memo, d, c in zip(f.getlist("account"), f.getlist("lname"), f.getlist("lmemo"),
                                       f.getlist("debit"), f.getlist("credit")):
            d, c = _amount(d or 0), _amount(c or 0)
            if not acc and not d and not c:
                continue
            lines.append((_account_id(acc), nm.strip(), memo.strip(), d, c))
        try:
            tid = _save("General Journal", parse_date(f.get("date"), date.today()), f.get("num", "").strip(),
                        "", f.get("memo", "").strip(), lines)
            flash("Journal entry saved.", "ok")
            return redirect(url_for("transaction", tid=tid))
        except ValueError as e:
            flash(str(e), "error")
    return render_template("form_journal.html", accounts=_account_options(), today=date.today().isoformat())


@app.route("/new/<kind>", methods=["GET", "POST"])
@login_required("accountant")
def new_doc(kind):
    """invoice / bill / payment (customer pays us) / billpay (we pay a vendor) / expense (direct cheque)."""
    specs = {
        "invoice": ("Invoice", "customers"), "bill": ("Bill", "vendors"), "payment": ("Payment", "customers"),
        "billpay": ("Bill Pmt -Check", "vendors"), "expense": ("Check", "vendors"),
    }
    if kind not in specs:
        abort(404)
    ttype, party_kind = specs[kind]
    con = get_db()
    if request.method == "POST":
        f = request.form
        name = f.get("party", "").strip()
        tdate = parse_date(f.get("date"), date.today())
        num, memo = f.get("num", "").strip(), f.get("memo", "").strip()
        try:
            if not name and kind != "expense":
                raise ValueError("Choose a " + party_kind[:-1] + ".")
            if kind in ("invoice", "bill", "expense"):
                rows = []
                for acc, desc, amt in zip(f.getlist("account"), f.getlist("desc"), f.getlist("amount")):
                    a = _amount(amt or 0)
                    if a:
                        rows.append((_account_id(acc), desc.strip(), a))
                total = round(sum(r[2] for r in rows), 2)
                if kind == "invoice":
                    lines = [(_first_account("Accounts Receivable"), name, memo, total, 0.0)]
                    lines += [(a, name, d, 0.0, amt) for a, d, amt in rows]
                elif kind == "bill":
                    lines = [(_first_account("Accounts Payable"), name, memo, 0.0, total)]
                    lines += [(a, name, d, amt, 0.0) for a, d, amt in rows]
                else:
                    lines = [(_account_id(f.get("bank")), name, memo, 0.0, total)]
                    lines += [(a, name, d, amt, 0.0) for a, d, amt in rows]
            else:
                amt = _amount(f.get("amount") or 0)
                bank = _account_id(f.get("bank"))
                if kind == "payment":
                    lines = [(bank, name, memo, amt, 0.0), (_first_account("Accounts Receivable"), name, memo, 0.0, amt)]
                else:
                    lines = [(_first_account("Accounts Payable"), name, memo, amt, 0.0), (bank, name, memo, 0.0, amt)]
            tid = _save(ttype, tdate, num, name, memo, lines)
            flash(f"{ttype} saved.", "ok")
            return redirect(url_for("transaction", tid=tid))
        except ValueError as e:
            flash(str(e), "error")
    parties = [r[0] for r in con.execute(f"SELECT full_name FROM {party_kind} WHERE active=1 ORDER BY full_name")]
    line_types = {"invoice": db.INCOME_TYPES, "bill": db.COGS_TYPES + db.EXPENSE_TYPES + db.ASSET_TYPES,
                  "expense": db.COGS_TYPES + db.EXPENSE_TYPES + db.ASSET_TYPES}.get(kind)
    item_rows = []
    if kind == "invoice":
        item_rows = con.execute("SELECT full_name, description, price, income_account FROM items WHERE active=1 "
                                "ORDER BY full_name").fetchall()
    return render_template("form_doc.html", kind=kind, ttype=ttype, party_kind=party_kind, parties=parties,
                           accounts=_account_options(line_types) if line_types else [],
                           banks=_account_options(("Bank", "Credit Card")), items=item_rows,
                           today=date.today().isoformat())


@app.route("/new/<kind>/add", methods=["POST"])
@login_required("accountant")
def add_party(kind):
    if kind not in ("customers", "vendors"):
        abort(404)
    name = request.form.get("full_name", "").strip()
    if not name:
        flash("Name is required.", "error")
    else:
        try:
            get_db().execute(f"INSERT INTO {kind}(full_name, name, company, phone, email, address) VALUES (?,?,?,?,?,?)"
                             if kind == "customers" else
                             f"INSERT INTO {kind}(full_name, name, company, phone, address) VALUES (?,?,?,?,?)",
                             ((name, name.split(':')[-1], request.form.get("company"), request.form.get("phone"),
                               request.form.get("email"), request.form.get("address")) if kind == "customers" else
                              (name, name.split(':')[-1], request.form.get("company"), request.form.get("phone"),
                               request.form.get("address"))))
            audit("add " + kind[:-1], name)
            get_db().commit()
            flash(f"Added {name}.", "ok")
        except Exception:
            flash("That name already exists.", "error")
    return redirect(url_for(kind))


# ---------------------------------------------------------------- admin

@app.route("/admin/users", methods=["GET", "POST"])
@login_required("admin")
def users():
    con = get_db()
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action == "create":
            u, pw, role = f.get("username", "").strip(), f.get("password", ""), f.get("role")
            if not u or len(pw) < 8 or role not in ROLES:
                flash("Username, a role and a password of at least 8 characters are required.", "error")
            else:
                try:
                    con.execute("INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)",
                                (u, f.get("full_name", "").strip(), generate_password_hash(pw), role))
                    audit("create user", f"{u} ({role})")
                    con.commit()
                    flash(f"User {u} created. Give them their password privately; they can change it after signing in.", "ok")
                except Exception:
                    flash("That username is taken.", "error")
        elif action in ("role", "toggle", "reset"):
            uid = f.get("uid", type=int)
            target = con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone() or abort(404)
            if uid == current_user()["id"] and action != "reset":
                flash("You can't change your own role or disable yourself.", "error")
            elif action == "role" and f.get("role") in ROLES:
                con.execute("UPDATE users SET role=? WHERE id=?", (f["role"], uid))
                audit("change role", f"{target['username']} -> {f['role']}")
            elif action == "toggle":
                con.execute("UPDATE users SET active=1-active WHERE id=?", (uid,))
                audit("enable/disable user", target["username"])
            elif action == "reset":
                pw = f.get("password", "")
                if len(pw) < 8:
                    flash("Password must be at least 8 characters.", "error")
                else:
                    con.execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), uid))
                    audit("reset password", target["username"])
                    flash(f"Password reset for {target['username']}.", "ok")
            con.commit()
        return redirect(url_for("users"))
    rows = con.execute("SELECT * FROM users ORDER BY username").fetchall()
    log = con.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("users.html", rows=rows, log=log)


@app.errorhandler(403)
def _forbidden(_e):
    return render_template("error.html", code=403, msg="Your role doesn't allow this. Ask an admin."), 403


@app.errorhandler(404)
def _notfound(_e):
    return render_template("error.html", code=404, msg="Not found."), 404


@app.errorhandler(400)
def _bad(e):
    return render_template("error.html", code=400, msg=getattr(e, "description", "Bad request")), 400


with app.app_context():
    _c = db.connect(); db.init(_c); _c.close()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", 8080)), debug=False)
