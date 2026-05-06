from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, session
)
import sqlite3
import os
import requests
import threading
import time
import hashlib
from datetime import datetime, date
from functools import wraps

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY", "andyos-local-dev-key"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, "database.db")


# ============================================================
# LOGIN CONFIG
# ============================================================

LOGIN_USERNAME = os.environ.get("LOGIN_USERNAME", "admin")
LOGIN_PASSWORD = os.environ.get("LOGIN_PASSWORD", "andyos123")


# ============================================================
# LOGIN HELPERS
# ============================================================

def is_logged_in():
    return session.get("logged_in") is True


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ============================================================
# TELEGRAM CONFIG
# ============================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID   = os.environ.get("CHAT_ID", "")


def send_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id"    : CHAT_ID,
            "text"       : message,
            "parse_mode" : "HTML"
        }
        response = requests.post(url, data=payload, timeout=5)
        if response.status_code == 200:
            print("Telegram sent successfully.")
        else:
            print(f"Telegram error: {response.text}")
    except Exception as e:
        print(f"Telegram failed (app still works): {e}")


# ============================================================
# DAILY BRIEFING
# ============================================================

def build_daily_briefing():
    conn      = get_db()
    today_str = str(date.today())
    now       = datetime.now()
    day_name  = now.strftime("%A")
    date_str  = now.strftime("%d %B %Y")

    lines = [
        f"🌅 <b>Good Morning Andy!</b>",
        f"🏠 <b>AndyOS Daily Briefing</b>",
        f"📅 {day_name}, {date_str}",
        ""
    ]

    inventory = conn.execute(
        "SELECT * FROM inventory ORDER BY name"
    ).fetchall()

    low_stock = []
    ok_stock  = []

    for item in inventory:
        if item["regular_stock"] > 0:
            pct = (item["current_stock"] / item["regular_stock"]) * 100
            if pct <= 25:
                low_stock.append((item, int(pct)))
            else:
                ok_stock.append((item, int(pct)))

    if low_stock:
        lines.append("⚠️ <b>LOW STOCK — Action Needed:</b>")
        for item, pct in low_stock:
            lines.append(
                f"  • {item['name']}: {item['current_stock']}"
                f" {item['unit']} ({pct}% left)"
            )
        lines.append("")

    if ok_stock:
        lines.append("📦 <b>Stock Levels OK:</b>")
        for item, pct in ok_stock:
            lines.append(
                f"  • {item['name']}: {item['current_stock']}"
                f" {item['unit']} ({pct}%)"
            )
        lines.append("")

    if not inventory:
        lines.append("📦 No inventory items tracked yet.")
        lines.append("")

    upcoming_renewals = []
    for item in inventory:
        if item["renewal_date"]:
            try:
                renewal   = datetime.strptime(
                    item["renewal_date"], "%Y-%m-%d"
                ).date()
                days_away = (renewal - date.today()).days
                if 0 <= days_away <= 7:
                    upcoming_renewals.append((item, days_away))
            except:
                pass

    if upcoming_renewals:
        lines.append("📆 <b>Renewals This Week:</b>")
        for item, days in upcoming_renewals:
            if days == 0:
                label = "today"
            elif days == 1:
                label = "tomorrow"
            else:
                label = f"in {days} days"
            lines.append(f"  • {item['name']} — due {label}")
        lines.append("")

    due_today = conn.execute("""
        SELECT * FROM reminders
        WHERE done = 0 AND due_date = ?
        ORDER BY priority DESC
    """, (today_str,)).fetchall()

    if due_today:
        lines.append("🔔 <b>Due TODAY:</b>")
        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        for r in due_today:
            emoji = priority_emoji.get(r["priority"], "•")
            lines.append(f"  {emoji} {r['title']}")
        lines.append("")

    overdue = conn.execute("""
        SELECT * FROM reminders
        WHERE done = 0 AND due_date < ?
        ORDER BY due_date ASC
    """, (today_str,)).fetchall()

    if overdue:
        lines.append("❌ <b>OVERDUE:</b>")
        for r in overdue:
            lines.append(
                f"  • {r['title']} (was due {r['due_date']})"
            )
        lines.append("")

    upcoming_reminders = conn.execute("""
        SELECT * FROM reminders
        WHERE done = 0
          AND due_date > ?
          AND due_date <= date(?, '+7 days')
        ORDER BY due_date ASC
    """, (today_str, today_str)).fetchall()

    if upcoming_reminders:
        lines.append("📋 <b>Coming Up This Week:</b>")
        for r in upcoming_reminders:
            lines.append(f"  • {r['title']} — {r['due_date']}")
        lines.append("")

    month_start   = (
        f"{date.today().year}-{date.today().month:02d}-01"
    )
    monthly_total = conn.execute(
        "SELECT SUM(amount) FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()[0] or 0

    expense_count = conn.execute(
        "SELECT COUNT(*) FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()[0] or 0

    lines.append("💰 <b>Spending This Month:</b>")
    lines.append(f"  • Total: ${monthly_total:.2f}")
    lines.append(f"  • Transactions: {expense_count}")
    lines.append("")
    lines.append(
        f"🕐 Briefing sent at {now.strftime('%H:%M')}"
    )

    conn.close()
    return "\n".join(lines)


# ============================================================
# SCHEDULER
# ============================================================

def run_scheduler():
    print("Scheduler started — daily briefing set for 08:00")
    last_sent_date = None

    while True:
        now    = datetime.now()
        hour   = now.hour
        minute = now.minute
        today  = now.date()

        if hour == 8 and minute == 0 and last_sent_date != today:
            print("Sending daily briefing...")
            message = build_daily_briefing()
            send_telegram(message)
            last_sent_date = today
            print("Daily briefing sent.")

        time.sleep(55)


def start_scheduler():
    thread = threading.Thread(target=run_scheduler, daemon=True)
    thread.start()


# ============================================================
# DATABASE SETUP
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn   = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            current_stock REAL NOT NULL,
            regular_stock REAL NOT NULL,
            unit TEXT NOT NULL,
            renewal_date TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            amount REAL NOT NULL,
            category TEXT NOT NULL,
            date TEXT NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            due_date TEXT NOT NULL,
            priority TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()
    print("Database ready at:", DB_PATH)


# ============================================================
# ALERT HELPERS
# ============================================================

def check_and_alert():
    conn           = get_db()
    inventory      = conn.execute(
        "SELECT * FROM inventory"
    ).fetchall()
    low_stock_msgs = []

    for item in inventory:
        if item["regular_stock"] > 0:
            pct = (item["current_stock"] / item["regular_stock"]) * 100
            if pct <= 25:
                low_stock_msgs.append(
                    f"  • {item['name']}: {item['current_stock']}"
                    f" {item['unit']} ({int(pct)}% left)"
                )

    today_str = str(date.today())
    overdue   = conn.execute(
        "SELECT * FROM reminders WHERE done = 0 AND due_date < ?",
        (today_str,)
    ).fetchall()

    overdue_msgs = []
    for r in overdue:
        overdue_msgs.append(
            f"  • {r['title']} (was due {r['due_date']})"
        )

    conn.close()

    if low_stock_msgs or overdue_msgs:
        lines = ["🏠 <b>AndyOS Alert</b>"]

        if low_stock_msgs:
            lines.append("")
            lines.append("📦 <b>Low Stock:</b>")
            lines.extend(low_stock_msgs)

        if overdue_msgs:
            lines.append("")
            lines.append("🔔 <b>Overdue Reminders:</b>")
            lines.extend(overdue_msgs)

        lines.append("")
        lines.append(
            f"🕐 Checked at "
            f"{datetime.now().strftime('%H:%M on %d %b %Y')}"
        )
        send_telegram("\n".join(lines))


def should_send_alert():
    conn = get_db()
    last = conn.execute(
        "SELECT sent_at FROM alert_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if last is None:
        return True

    last_time   = datetime.fromisoformat(last["sent_at"])
    hours_since = (datetime.now() - last_time).total_seconds() / 3600
    return hours_since >= 6


def log_alert_sent():
    conn = get_db()
    conn.execute(
        "INSERT INTO alert_log (sent_at) VALUES (?)",
        (datetime.now().isoformat(),)
    )
    conn.commit()
    conn.close()


# ============================================================
# ROUTES — LOGIN / LOGOUT
# ============================================================

@app.route("/login", methods=["GET", "POST"])
def login():
    if is_logged_in():
        return redirect(url_for("home"))

    error = None

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
            session["logged_in"] = True
            session["username"]  = username

            send_telegram(
                f"🔐 <b>AndyOS Login</b>\n"
                f"User <b>{username}</b> logged in\n"
                f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
            )

            return redirect(url_for("home"))
        else:
            error = "Wrong username or password. Try again."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    username = session.get("username", "User")
    session.clear()

    send_telegram(
        f"🔓 <b>AndyOS Logout</b>\n"
        f"User <b>{username}</b> logged out\n"
        f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
    )

    return redirect(url_for("login"))


# ============================================================
# ROUTES — DASHBOARD
# ============================================================

@app.route("/")
@login_required
def home():
    conn   = get_db()
    cursor = conn.cursor()

    inventory = cursor.execute(
        "SELECT * FROM inventory ORDER BY name"
    ).fetchall()

    expenses = cursor.execute(
        "SELECT * FROM expenses ORDER BY date DESC LIMIT 5"
    ).fetchall()

    reminders = cursor.execute(
        "SELECT * FROM reminders WHERE done = 0 ORDER BY due_date ASC"
    ).fetchall()

    today       = date.today()
    month_start = f"{today.year}-{today.month:02d}-01"
    monthly_total = cursor.execute(
        "SELECT SUM(amount) FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()[0] or 0

    conn.close()

    low_stock = []
    for item in inventory:
        if item["regular_stock"] > 0:
            percentage = (
                item["current_stock"] / item["regular_stock"]
            ) * 100
            if percentage <= 25:
                low_stock.append(item)

    today_str = str(date.today())
    overdue   = [r for r in reminders if r["due_date"] < today_str]

    if should_send_alert():
        check_and_alert()
        log_alert_sent()

    return render_template(
        "home.html",
        inventory=inventory,
        expenses=expenses,
        reminders=reminders,
        monthly_total=monthly_total,
        low_stock=low_stock,
        overdue=overdue,
        today=today_str
    )


# ============================================================
# ROUTES — ADD
# ============================================================

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_item():
    if request.method == "POST":
        name          = request.form.get("name", "").strip()
        category      = request.form.get("category", "").strip()
        current_stock = request.form.get("current_stock", 0)
        regular_stock = request.form.get("regular_stock", 0)
        unit          = request.form.get("unit", "").strip()
        renewal_date  = request.form.get("renewal_date", "").strip()
        notes         = request.form.get("notes", "").strip()

        if not name or not category or not unit:
            return render_template(
                "add.html",
                error="Name, category, and unit are required.",
                section="inventory"
            )

        conn = get_db()
        conn.execute("""
            INSERT INTO inventory
            (name, category, current_stock, regular_stock,
             unit, renewal_date, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            name, category, float(current_stock),
            float(regular_stock), unit,
            renewal_date or None, notes or None
        ))
        conn.commit()
        conn.close()

        send_telegram(
            f"✅ <b>New Inventory Item Added</b>\n\n"
            f"📦 <b>{name}</b>\n"
            f"Category: {category}\n"
            f"Stock: {current_stock} / {regular_stock} {unit}"
            + (f"\nRenewal: {renewal_date}" if renewal_date else "")
        )

        return redirect(url_for("home"))

    return render_template("add.html", section="inventory")


@app.route("/add-expense", methods=["GET", "POST"])
@login_required
def add_expense():
    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        amount   = request.form.get("amount", 0)
        category = request.form.get("category", "").strip()
        exp_date = request.form.get("date", "").strip()
        notes    = request.form.get("notes", "").strip()

        if not title or not category or not exp_date:
            return render_template(
                "add.html",
                error="Title, category, and date are required.",
                section="expense"
            )

        conn = get_db()
        conn.execute("""
            INSERT INTO expenses
            (title, amount, category, date, notes)
            VALUES (?, ?, ?, ?, ?)
        """, (
            title, float(amount), category,
            exp_date, notes or None
        ))
        conn.commit()
        conn.close()

        send_telegram(
            f"💰 <b>New Expense Recorded</b>\n\n"
            f"📝 <b>{title}</b>\n"
            f"Amount: <b>${float(amount):.2f}</b>\n"
            f"Category: {category}\n"
            f"Date: {exp_date}"
        )

        return redirect(url_for("home"))

    return render_template("add.html", section="expense")


@app.route("/add-reminder", methods=["GET", "POST"])
@login_required
def add_reminder():
    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        due_date = request.form.get("due_date", "").strip()
        priority = request.form.get("priority", "medium").strip()
        notes    = request.form.get("notes", "").strip()

        if not title or not due_date:
            return render_template(
                "add.html",
                error="Title and due date are required.",
                section="reminder"
            )

        conn = get_db()
        conn.execute("""
            INSERT INTO reminders
            (title, due_date, priority, notes)
            VALUES (?, ?, ?, ?)
        """, (title, due_date, priority, notes or None))
        conn.commit()
        conn.close()

        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        emoji = priority_emoji.get(priority, "🔔")

        send_telegram(
            f"🔔 <b>New Reminder Set</b>\n\n"
            f"{emoji} <b>{title}</b>\n"
            f"Due: {due_date}\n"
            f"Priority: {priority.capitalize()}"
        )

        return redirect(url_for("home"))

    return render_template("add.html", section="reminder")


# ============================================================
# ROUTES — EDIT
# ============================================================

@app.route("/edit-inventory/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit_inventory(item_id):
    conn = get_db()

    if request.method == "POST":
        name          = request.form.get("name", "").strip()
        category      = request.form.get("category", "").strip()
        current_stock = request.form.get("current_stock", 0)
        regular_stock = request.form.get("regular_stock", 0)
        unit          = request.form.get("unit", "").strip()
        renewal_date  = request.form.get("renewal_date", "").strip()
        notes         = request.form.get("notes", "").strip()

        if not name or not category or not unit:
            item = conn.execute(
                "SELECT * FROM inventory WHERE id = ?", (item_id,)
            ).fetchone()
            conn.close()
            return render_template(
                "edit.html", item=item, section="inventory",
                error="Name, category, and unit are required."
            )

        conn.execute("""
            UPDATE inventory
            SET name=?, category=?, current_stock=?,
                regular_stock=?, unit=?, renewal_date=?, notes=?
            WHERE id=?
        """, (
            name, category, float(current_stock),
            float(regular_stock), unit,
            renewal_date or None, notes or None, item_id
        ))
        conn.commit()
        conn.close()

        send_telegram(
            f"✏️ <b>Inventory Item Updated</b>\n\n"
            f"📦 <b>{name}</b>\n"
            f"Stock: {current_stock} / {regular_stock} {unit}"
        )

        return redirect(url_for("home"))

    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()

    if not item:
        return redirect(url_for("home"))

    return render_template(
        "edit.html", item=item, section="inventory"
    )


@app.route("/edit-expense/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit_expense(item_id):
    conn = get_db()

    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        amount   = request.form.get("amount", 0)
        category = request.form.get("category", "").strip()
        exp_date = request.form.get("date", "").strip()
        notes    = request.form.get("notes", "").strip()

        if not title or not category or not exp_date:
            item = conn.execute(
                "SELECT * FROM expenses WHERE id = ?", (item_id,)
            ).fetchone()
            conn.close()
            return render_template(
                "edit.html", item=item, section="expense",
                error="Title, category, and date are required."
            )

        conn.execute("""
            UPDATE expenses
            SET title=?, amount=?, category=?, date=?, notes=?
            WHERE id=?
        """, (
            title, float(amount), category,
            exp_date, notes or None, item_id
        ))
        conn.commit()
        conn.close()

        send_telegram(
            f"✏️ <b>Expense Updated</b>\n\n"
            f"📝 <b>{title}</b>\n"
            f"Amount: <b>${float(amount):.2f}</b>\n"
            f"Date: {exp_date}"
        )

        return redirect(url_for("home"))

    item = conn.execute(
        "SELECT * FROM expenses WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()

    if not item:
        return redirect(url_for("home"))

    return render_template(
        "edit.html", item=item, section="expense"
    )


@app.route("/edit-reminder/<int:item_id>", methods=["GET", "POST"])
@login_required
def edit_reminder(item_id):
    conn = get_db()

    if request.method == "POST":
        title    = request.form.get("title", "").strip()
        due_date = request.form.get("due_date", "").strip()
        priority = request.form.get("priority", "medium").strip()
        notes    = request.form.get("notes", "").strip()

        if not title or not due_date:
            item = conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (item_id,)
            ).fetchone()
            conn.close()
            return render_template(
                "edit.html", item=item, section="reminder",
                error="Title and due date are required."
            )

        conn.execute("""
            UPDATE reminders
            SET title=?, due_date=?, priority=?, notes=?
            WHERE id=?
        """, (
            title, due_date, priority,
            notes or None, item_id
        ))
        conn.commit()
        conn.close()

        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        emoji = priority_emoji.get(priority, "🔔")

        send_telegram(
            f"✏️ <b>Reminder Updated</b>\n\n"
            f"{emoji} <b>{title}</b>\n"
            f"Due: {due_date}\n"
            f"Priority: {priority.capitalize()}"
        )

        return redirect(url_for("home"))

    item = conn.execute(
        "SELECT * FROM reminders WHERE id = ?", (item_id,)
    ).fetchone()
    conn.close()

    if not item:
        return redirect(url_for("home"))

    return render_template(
        "edit.html", item=item, section="reminder"
    )


# ============================================================
# ROUTES — DELETE / COMPLETE / UPDATE STOCK
# ============================================================

@app.route("/delete/<table>/<int:item_id>")
@login_required
def delete_item(table, item_id):
    allowed_tables = ["inventory", "expenses", "reminders"]
    if table not in allowed_tables:
        return "Not allowed", 403

    conn = get_db()
    item = conn.execute(
        f"SELECT * FROM {table} WHERE id = ?", (item_id,)
    ).fetchone()

    conn.execute(
        f"DELETE FROM {table} WHERE id = ?", (item_id,)
    )
    conn.commit()
    conn.close()

    if item:
        name_field = (
            item["name"]  if "name"  in item.keys() else
            item["title"] if "title" in item.keys() else
            "Item"
        )
        send_telegram(
            f"🗑️ <b>Deleted from {table.capitalize()}</b>\n"
            f"Removed: <b>{name_field}</b>"
        )

    return redirect(url_for("home"))


@app.route("/complete-reminder/<int:item_id>")
@login_required
def complete_reminder(item_id):
    conn = get_db()

    reminder = conn.execute(
        "SELECT title FROM reminders WHERE id = ?", (item_id,)
    ).fetchone()

    conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ?", (item_id,)
    )
    conn.commit()
    conn.close()

    if reminder:
        send_telegram(
            f"✅ <b>Reminder Completed!</b>\n"
            f"Task: <b>{reminder['title']}</b>"
        )

    return redirect(url_for("home"))


@app.route("/update-stock/<int:item_id>", methods=["POST"])
@login_required
def update_stock(item_id):
    new_stock = request.form.get("current_stock", 0)

    conn = get_db()
    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()

    conn.execute(
        "UPDATE inventory SET current_stock = ? WHERE id = ?",
        (float(new_stock), item_id)
    )
    conn.commit()
    conn.close()

    if item and item["regular_stock"] > 0:
        new_pct = (float(new_stock) / item["regular_stock"]) * 100
        old_pct = (
            item["current_stock"] / item["regular_stock"]
        ) * 100

        if new_pct <= 25 and old_pct > 25:
            send_telegram(
                f"⚠️ <b>Stock Alert!</b>\n\n"
                f"📦 <b>{item['name']}</b> just dropped below 25%\n"
                f"Current: {new_stock} {item['unit']}\n"
                f"That is {int(new_pct)}% of normal stock"
            )

    return redirect(url_for("home"))


# ============================================================
# ROUTES — ANALYTICS
# ============================================================

@app.route("/analytics")
@login_required
def analytics():
    conn = get_db()

    category_spend = conn.execute("""
        SELECT category, SUM(amount) as total
        FROM expenses
        GROUP BY category
        ORDER BY total DESC
    """).fetchall()

    today       = date.today()
    month_start = f"{today.year}-{today.month:02d}-01"

    daily_spend = conn.execute("""
        SELECT date, SUM(amount) as total
        FROM expenses
        WHERE date >= ?
        GROUP BY date
        ORDER BY date ASC
    """, (month_start,)).fetchall()

    monthly_totals = conn.execute("""
        SELECT
            strftime('%Y-%m', date) as month,
            SUM(amount) as total
        FROM expenses
        GROUP BY month
        ORDER BY month DESC
        LIMIT 6
    """).fetchall()
    monthly_totals = list(reversed(monthly_totals))

    inventory = conn.execute(
        "SELECT * FROM inventory ORDER BY name"
    ).fetchall()

    total_reminders = conn.execute(
        "SELECT COUNT(*) FROM reminders"
    ).fetchone()[0] or 0

    done_reminders = conn.execute(
        "SELECT COUNT(*) FROM reminders WHERE done = 1"
    ).fetchone()[0] or 0

    pending_reminders = conn.execute(
        "SELECT COUNT(*) FROM reminders WHERE done = 0"
    ).fetchone()[0] or 0

    overdue_reminders = conn.execute(
        "SELECT COUNT(*) FROM reminders WHERE done=0 AND due_date < ?",
        (str(today),)
    ).fetchone()[0] or 0

    all_time_total = conn.execute(
        "SELECT SUM(amount) FROM expenses"
    ).fetchone()[0] or 0

    total_transactions = conn.execute(
        "SELECT COUNT(*) FROM expenses"
    ).fetchone()[0] or 0

    monthly_this_month = conn.execute(
        "SELECT SUM(amount) FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()[0] or 0

    avg_transaction = (
        all_time_total / total_transactions
        if total_transactions > 0 else 0
    )

    conn.close()

    cat_labels   = [row["category"] for row in category_spend]
    cat_values   = [row["total"]    for row in category_spend]
    day_labels   = [row["date"]     for row in daily_spend]
    day_values   = [row["total"]    for row in daily_spend]
    month_labels = [row["month"]    for row in monthly_totals]
    month_values = [row["total"]    for row in monthly_totals]

    stock_labels  = [item["name"]          for item in inventory]
    stock_current = [item["current_stock"] for item in inventory]
    stock_regular = [item["regular_stock"] for item in inventory]

    stock_pct = []
    for item in inventory:
        if item["regular_stock"] > 0:
            pct = (
                item["current_stock"] / item["regular_stock"]
            ) * 100
            stock_pct.append(round(pct, 1))
        else:
            stock_pct.append(0)

    return render_template(
        "analytics.html",
        cat_labels         = cat_labels,
        cat_values         = cat_values,
        day_labels         = day_labels,
        day_values         = day_values,
        month_labels       = month_labels,
        month_values       = month_values,
        stock_labels       = stock_labels,
        stock_current      = stock_current,
        stock_regular      = stock_regular,
        stock_pct          = stock_pct,
        all_time_total     = all_time_total,
        total_transactions = total_transactions,
        monthly_this_month = monthly_this_month,
        avg_transaction    = avg_transaction,
        total_reminders    = total_reminders,
        done_reminders     = done_reminders,
        pending_reminders  = pending_reminders,
        overdue_reminders  = overdue_reminders,
        inventory_count    = len(inventory),
    )


# ============================================================
# ROUTES — TELEGRAM TEST + MANUAL BRIEFING
# ============================================================

@app.route("/test-telegram")
@login_required
def test_telegram():
    send_telegram(
        "🏠 <b>AndyOS Test Message</b>\n\n"
        "✅ Your Telegram bot is connected and working!\n"
        f"🕐 Sent at {datetime.now().strftime('%H:%M on %d %b %Y')}"
    )
    return """
        <h2 style='font-family:sans-serif; color:#51cf66;'>
            ✅ Test message sent!
        </h2>
        <p style='font-family:sans-serif;'>Check your Telegram.</p>
        <a href='/' style='font-family:sans-serif;'>
            ← Back to Dashboard
        </a>
    """


@app.route("/send-briefing")
@login_required
def send_briefing():
    message = build_daily_briefing()
    send_telegram(message)
    return """
        <h2 style='font-family:sans-serif; color:#51cf66;'>
            ✅ Daily briefing sent to Telegram!
        </h2>
        <p style='font-family:sans-serif;'>
            Check your Telegram to see the full briefing.
        </p>
        <a href='/' style='font-family:sans-serif;'>
            ← Back to Dashboard
        </a>
    """


# ============================================================
# RUN THE APP
# ============================================================

if __name__ == "__main__":
    init_db()
    start_scheduler()
    port = int(os.environ.get("PORT", 5000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False
    )