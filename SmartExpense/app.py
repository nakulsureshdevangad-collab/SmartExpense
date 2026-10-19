import os
import sqlite3
from datetime import date

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from werkzeug.security import generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-change-this-secret-key")
app.config["DATABASE"] = os.path.join(app.instance_path, "smartexpense.db")
os.makedirs(app.instance_path, exist_ok=True)


def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    db().executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            kind TEXT NOT NULL CHECK(kind IN ('income','expense')),
            category TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            txn_date TEXT NOT NULL,
            description TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS budgets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            amount REAL NOT NULL CHECK(amount > 0),
            month TEXT NOT NULL,
            UNIQUE(user_id, category, month)
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            target REAL NOT NULL CHECK(target > 0),
            saved REAL NOT NULL DEFAULT 0 CHECK(saved >= 0),
            created_at TEXT NOT NULL
        );
    """)
    db().commit()


@app.before_request
def ensure_db():
    init_db()
    # This is a single-user local app, so open the existing workspace directly.
    g.user = db().execute("SELECT id, name, email FROM users ORDER BY id LIMIT 1").fetchone()
    if g.user is None:
        cur = db().execute(
            "INSERT INTO users(name,email,password_hash) VALUES(?,?,?)",
            ("My Workspace", "local@smartexpense", generate_password_hash(os.urandom(24).hex())),
        )
        db().commit()
        g.user = db().execute("SELECT id, name, email FROM users WHERE id=?", (cur.lastrowid,)).fetchone()


def current_month():
    return date.today().strftime("%Y-%m")


def money(value):
    return f"₹{value:,.2f}"


app.jinja_env.filters["money"] = money


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    month = current_month()
    rows = db().execute("SELECT kind, COALESCE(SUM(amount),0) total FROM transactions WHERE user_id=? AND substr(txn_date,1,7)=? GROUP BY kind",
                        (g.user["id"], month)).fetchall()
    totals = {row["kind"]: row["total"] for row in rows}
    income, expenses = totals.get("income", 0), totals.get("expense", 0)
    budgets = db().execute("""SELECT b.category,b.amount,COALESCE(SUM(t.amount),0) spent
        FROM budgets b LEFT JOIN transactions t ON t.user_id=b.user_id AND t.category=b.category
        AND t.kind='expense' AND substr(t.txn_date,1,7)=b.month
        WHERE b.user_id=? AND b.month=? GROUP BY b.id ORDER BY b.category""", (g.user["id"], month)).fetchall()
    categories = db().execute("SELECT category,SUM(amount) total FROM transactions WHERE user_id=? AND kind='expense' AND substr(txn_date,1,7)=? GROUP BY category ORDER BY total DESC",
                              (g.user["id"], month)).fetchall()
    recent = db().execute("SELECT * FROM transactions WHERE user_id=? ORDER BY txn_date DESC,id DESC LIMIT 8", (g.user["id"],)).fetchall()
    goals = db().execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    return render_template("dashboard.html", month=month, income=income, expenses=expenses,
                           balance=income-expenses, budgets=budgets, categories=categories,
                           recent=recent, goals=goals)


@app.route("/transactions", methods=["GET", "POST"])
def transactions():
    if request.method == "POST":
        kind = request.form.get("kind")
        category = request.form.get("category", "").strip()
        description = request.form.get("description", "").strip()
        txn_date = request.form.get("txn_date", "")
        try:
            amount = float(request.form.get("amount", ""))
            date.fromisoformat(txn_date)
            if kind not in ("income", "expense") or not category or amount <= 0:
                raise ValueError
            db().execute("INSERT INTO transactions(user_id,kind,category,amount,txn_date,description) VALUES(?,?,?,?,?,?)",
                         (g.user["id"], kind, category, amount, txn_date, description))
            db().commit()
            flash("Transaction added.", "success")
            return redirect(url_for("transactions"))
        except (ValueError, TypeError):
            flash("Enter a valid type, category, date, and amount greater than zero.", "error")
    rows = db().execute("SELECT * FROM transactions WHERE user_id=? ORDER BY txn_date DESC,id DESC", (g.user["id"],)).fetchall()
    return render_template("transactions.html", transactions=rows, today=date.today().isoformat())


@app.post("/transactions/<int:transaction_id>/delete")
def delete_transaction(transaction_id):
    db().execute("DELETE FROM transactions WHERE id=? AND user_id=?", (transaction_id, g.user["id"]))
    db().commit()
    flash("Transaction deleted.", "success")
    return redirect(url_for("transactions"))


@app.route("/budgets", methods=["GET", "POST"])
def budgets():
    if request.method == "POST":
        category = request.form.get("category", "").strip()
        month = request.form.get("month", "")
        try:
            amount = float(request.form.get("amount", ""))
            if not category or amount <= 0 or len(month) != 7:
                raise ValueError
            db().execute("INSERT INTO budgets(user_id,category,amount,month) VALUES(?,?,?,?) ON CONFLICT(user_id,category,month) DO UPDATE SET amount=excluded.amount",
                         (g.user["id"], category, amount, month))
            db().commit()
            flash("Budget saved.", "success")
            return redirect(url_for("budgets"))
        except (ValueError, TypeError):
            flash("Enter a category, month, and budget greater than zero.", "error")
    month = request.args.get("month", current_month())
    rows = db().execute("""SELECT b.*,COALESCE(SUM(t.amount),0) spent FROM budgets b
        LEFT JOIN transactions t ON t.user_id=b.user_id AND t.category=b.category AND t.kind='expense'
        AND substr(t.txn_date,1,7)=b.month WHERE b.user_id=? AND b.month=? GROUP BY b.id ORDER BY b.category""",
                        (g.user["id"], month)).fetchall()
    return render_template("budgets.html", budgets=rows, month=month)


@app.route("/goals", methods=["GET", "POST"])
def goals():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        try:
            target = float(request.form.get("target", ""))
            saved = float(request.form.get("saved", "0") or 0)
            if not name or target <= 0 or saved < 0:
                raise ValueError
            db().execute("INSERT INTO goals(user_id,name,target,saved,created_at) VALUES(?,?,?,?,?)",
                         (g.user["id"], name, target, saved, date.today().isoformat()))
            db().commit()
            flash("Savings goal created.", "success")
            return redirect(url_for("goals"))
        except (ValueError, TypeError):
            flash("Enter a goal name, positive target, and valid saved amount.", "error")
    rows = db().execute("SELECT * FROM goals WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    return render_template("goals.html", goals=rows)


@app.post("/goals/<int:goal_id>/update")
def update_goal(goal_id):
    try:
        saved = float(request.form.get("saved", ""))
        if saved < 0:
            raise ValueError
        cur = db().execute("UPDATE goals SET saved=? WHERE id=? AND user_id=?", (saved, goal_id, g.user["id"]))
        if not cur.rowcount:
            abort(404)
        db().commit()
        flash("Goal progress updated.", "success")
    except (ValueError, TypeError):
        flash("Enter a valid saved amount.", "error")
    return redirect(url_for("goals"))


@app.post("/goals/<int:goal_id>/delete")
def delete_goal(goal_id):
    db().execute("DELETE FROM goals WHERE id=? AND user_id=?", (goal_id, g.user["id"]))
    db().commit()
    flash("Goal deleted.", "success")
    return redirect(url_for("goals"))


@app.route("/reports")
def reports():
    month = current_month()
    by_category = db().execute("SELECT category,SUM(amount) total FROM transactions WHERE user_id=? AND kind='expense' AND substr(txn_date,1,7)=? GROUP BY category ORDER BY total DESC",
                               (g.user["id"], month)).fetchall()
    by_month = db().execute("SELECT substr(txn_date,1,7) month,kind,SUM(amount) total FROM transactions WHERE user_id=? AND txn_date>=date('now','-5 months','start of month') GROUP BY month,kind ORDER BY month",
                            (g.user["id"],)).fetchall()
    return render_template("reports.html", by_category=by_category,
                           by_month=[dict(row) for row in by_month], month=month)


if __name__ == "__main__":
    app.run(debug=True)
