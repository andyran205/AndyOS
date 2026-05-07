from flask import (
    Flask, render_template, request,
    redirect, url_for, flash, session
)
import sqlite3
import os
import json
import requests
import threading
import time
import hashlib
from datetime import datetime, date, timedelta
from functools import wraps
from pathlib import Path
import csv
import io

# ============================================================
# APP SETUP
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "SECRET_KEY", "andyos-local-dev-key"
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
def resolve_db_path():
    configured_db = os.environ.get("DATABASE_PATH", "").strip()
    if configured_db:
        return (
            configured_db
            if os.path.isabs(configured_db)
            else os.path.join(BASE_DIR, configured_db)
        )

    railway_volume = os.environ.get(
        "RAILWAY_VOLUME_MOUNT_PATH", ""
    ).strip()
    if railway_volume:
        return os.path.join(railway_volume, "database.db")

    return os.path.join(BASE_DIR, "database.db")


DB_PATH = resolve_db_path()
BACKUP_DIR = os.environ.get("ANDYOS_BACKUP_DIR", "").strip()
if not BACKUP_DIR:
    BACKUP_DIR = (
        os.path.join(os.path.dirname(DB_PATH), ".andyos")
        if os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
        else os.path.join(os.path.expanduser("~"), ".andyos")
    )
INVENTORY_BACKUP_PATH = os.path.join(
    BACKUP_DIR, "inventory_backup.json"
)
BACKUP_KEEP_COUNT = int(os.environ.get("ANDYOS_BACKUP_KEEP_COUNT", "20"))
DEFAULT_INVENTORY_ITEMS = [
    {"name": "Electricity bill", "category": "Utilities", "item_type": "quantifiable", "unit": "kWh", "current_stock": 0, "regular_stock": 0},
    {"name": "Water bill", "category": "Utilities", "item_type": "service", "frequency_days": 30},
    {"name": "Diesel", "category": "Utilities", "item_type": "quantifiable", "unit": "L", "current_stock": 0, "regular_stock": 0},
    {"name": "Septic", "category": "Maintenance", "item_type": "service", "frequency_days": 90},
    {"name": "Fish tank", "category": "Maintenance", "item_type": "service", "frequency_days": 30},
    {"name": "Fish food", "category": "Pet", "item_type": "quantifiable", "unit": "packs", "current_stock": 0, "regular_stock": 0},
    {"name": "Dog food Can", "category": "Pet", "item_type": "quantifiable", "unit": "cans", "current_stock": 0, "regular_stock": 0},
    {"name": "Dog food", "category": "Pet", "item_type": "quantifiable", "unit": "kg", "current_stock": 0, "regular_stock": 0},
    {"name": "Gas", "category": "Utilities", "item_type": "quantifiable", "unit": "kg", "current_stock": 0, "regular_stock": 0},
    {"name": "Fumigation", "category": "Maintenance", "item_type": "service", "frequency_days": 90},
    {"name": "Waste disposal", "category": "Utilities", "item_type": "service", "frequency_days": 30},
    {"name": "Internet", "category": "Utilities", "item_type": "service", "frequency_days": 30},
    {"name": "Dstv", "category": "Utilities", "item_type": "service", "frequency_days": 30},
    {"name": "Drinking water", "category": "Utilities", "item_type": "quantifiable", "unit": "L", "current_stock": 0, "regular_stock": 0},
    {"name": "AC servicing", "category": "Maintenance", "item_type": "service", "frequency_days": 90},
    {"name": "Manhole Servic", "category": "Maintenance", "item_type": "service", "frequency_days": 180},
]


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


def is_primary_admin_user(username):
    """The env-configured LOGIN_USERNAME acts as household owner / approver."""
    if not username:
        return False
    return username.strip().lower() == LOGIN_USERNAME.strip().lower()


def primary_admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not is_logged_in():
            return redirect(url_for("login"))
        if not is_primary_admin_user(session.get("username")):
            flash(
                "Only the primary administrator can manage account approvals.",
                "danger",
            )
            return redirect(url_for("home"))
        return f(*args, **kwargs)
    return decorated


def user_is_fully_active(user_row):
    """Existing accounts without the column behave as approved (grandfathered)."""
    if user_row is None:
        return False
    keys = user_row.keys()
    if "approved" not in keys:
        return True
    ap = user_row["approved"]
    if ap is None:
        return True
    try:
        return int(ap) == 1
    except (TypeError, ValueError):
        return False


def hash_password(password):
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


@app.context_processor
def inject_template_globals():
    uname = session.get("username") or ""
    return {
        "is_primary_admin": is_primary_admin_user(uname),
        "login_username": LOGIN_USERNAME,
    }


def get_user_by_username(conn, username):
    return conn.execute(
        "SELECT * FROM users WHERE LOWER(username) = LOWER(?)",
        (username,)
    ).fetchone()


# ============================================================
# WHATSAPP CONFIG
# ============================================================

WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "").strip()
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "").strip()
WHATSAPP_TO = os.environ.get("WHATSAPP_TO", "").strip()


def send_notification(message):
    try:
        if not (WHATSAPP_ACCESS_TOKEN and WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_TO):
            print("WhatsApp not configured; skipping notification.")
            return
        url = f"https://graph.facebook.com/v20.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"
        payload = {
            "messaging_product": "whatsapp",
            "to": WHATSAPP_TO,
            "type": "text",
            "text": {"body": message},
        }
        headers = {
            "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
            "Content-Type": "application/json",
        }
        response = requests.post(url, headers=headers, json=payload, timeout=8)
        if response.status_code in (200, 201):
            print("WhatsApp sent successfully.")
        else:
            print(f"WhatsApp error: {response.text}")
    except Exception as e:
        print(f"WhatsApp failed (app still works): {e}")


def send_telegram(message):
    # Backward-compatible wrapper: route all notifications to WhatsApp.
    send_notification(message)


# ============================================================
# STOCK HELPERS
# ============================================================

def get_days_until_empty(current_stock, avg_daily_use):
    """
    Calculate how many days until stock runs out.
    Returns None if cannot calculate.
    """
    try:
        if avg_daily_use and float(avg_daily_use) > 0:
            days = float(current_stock) / float(avg_daily_use)
            return round(days)
        return None
    except:
        return None


def get_effective_daily_use(item):
    try:
        if item["avg_daily_use"] and float(item["avg_daily_use"]) > 0:
            return float(item["avg_daily_use"])
    except Exception:
        pass
    try:
        if item["avg_daily_value"] and float(item["avg_daily_value"]) > 0:
            return float(item["avg_daily_value"])
    except Exception:
        pass
    return None


def get_calculated_stock(item):
    """
    For items in auto mode, calculate estimated stock continuously
    based on hours since last refill and average daily use.
    """
    try:
        effective_daily_use = get_effective_daily_use(item)
        if item["track_mode"] == "auto" and effective_daily_use:
            last_refill_raw = (
                item["last_refill_at"] or item["last_refill_date"]
            )
            if not last_refill_raw:
                return item["current_stock"]

            try:
                last_refill_dt = datetime.fromisoformat(last_refill_raw)
            except ValueError:
                # Backward compatibility for legacy YYYY-MM-DD values.
                last_refill_dt = datetime.strptime(
                    item["last_refill_date"], "%Y-%m-%d"
                )

            elapsed_hours = max(
                0.0,
                (datetime.now() - last_refill_dt).total_seconds() / 3600.0
            )
            used = elapsed_hours * (effective_daily_use / 24.0)
            estimated   = float(item["regular_stock"]) - used
            return max(0, round(estimated, 2))
        return item["current_stock"]
    except:
        return item["current_stock"]


# ============================================================
# SERVICE ITEM HELPERS
# ============================================================

def get_service_status(last_service_date, frequency_days):
    if not last_service_date or not frequency_days:
        return {
            "next_due"     : None,
            "days_until"   : None,
            "days_overdue" : None,
            "status"       : "unknown",
            "pct"          : 0
        }

    try:
        last     = datetime.strptime(
            last_service_date, "%Y-%m-%d"
        ).date()
        freq     = int(frequency_days)
        next_due = last + timedelta(days=freq)
        today    = date.today()
        days_until = (next_due - today).days

        if days_until < 0:
            status = "overdue"
            pct    = 100
        elif days_until <= 7:
            status = "due-soon"
            days_passed = (today - last).days
            pct = min(int((days_passed / freq) * 100), 100)
        else:
            days_passed = (today - last).days
            pct = min(int((days_passed / freq) * 100), 100)
            status = "ok"

        return {
            "next_due"     : str(next_due),
            "days_until"   : days_until,
            "days_overdue" : abs(days_until) if days_until < 0 else 0,
            "status"       : status,
            "pct"          : pct
        }

    except Exception as e:
        print(f"Service status error: {e}")
        return {
            "next_due"     : None,
            "days_until"   : None,
            "days_overdue" : None,
            "status"       : "unknown",
            "pct"          : 0
        }


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
        f"🏠 <b>Nku-OS Daily Briefing</b>",
        f"📅 {day_name}, {date_str}",
        ""
    ]

    inventory = conn.execute(
        "SELECT * FROM inventory WHERE item_type = 'quantifiable'"
        " ORDER BY name"
    ).fetchall()

    low_stock = []
    ok_stock  = []

    for item in inventory:
        current = get_calculated_stock(item)
        if item["regular_stock"] and item["regular_stock"] > 0:
            pct = (current / item["regular_stock"]) * 100
            if pct <= 25:
                low_stock.append((item, int(pct), current))
            else:
                ok_stock.append((item, int(pct), current))

    if low_stock:
        lines.append("⚠️ <b>LOW STOCK — Action Needed:</b>")
        for item, pct, current in low_stock:
            days_left = get_days_until_empty(
                current, get_effective_daily_use(item)
            )
            line = (
                f"  • {item['name']}: {current}"
                f" {item['unit']} ({pct}% left)"
            )
            if days_left is not None:
                line += f" — runs out in {days_left} days"
            lines.append(line)
        lines.append("")

    if ok_stock:
        lines.append("📦 <b>Stock Levels OK:</b>")
        for item, pct, current in ok_stock:
            days_left = get_days_until_empty(
                current, get_effective_daily_use(item)
            )
            line = (
                f"  • {item['name']}: {current}"
                f" {item['unit']} ({pct}%)"
            )
            if days_left is not None:
                line += f" — {days_left} days left"
            lines.append(line)
        lines.append("")

    if not inventory:
        lines.append("📦 No inventory items tracked yet.")
        lines.append("")

    services     = conn.execute(
        "SELECT * FROM inventory WHERE item_type = 'service'"
        " ORDER BY name"
    ).fetchall()

    overdue_svcs  = []
    due_soon_svcs = []

    for svc in services:
        status = get_service_status(
            svc["last_service_date"], svc["frequency_days"]
        )
        if status["status"] == "overdue":
            overdue_svcs.append((svc, status))
        elif status["status"] == "due-soon":
            due_soon_svcs.append((svc, status))

    if overdue_svcs:
        lines.append("🔧 <b>SERVICES OVERDUE:</b>")
        for svc, status in overdue_svcs:
            lines.append(
                f"  • {svc['name']} — "
                f"{status['days_overdue']} days overdue"
            )
        lines.append("")

    if due_soon_svcs:
        lines.append("🔧 <b>Services Due Soon:</b>")
        for svc, status in due_soon_svcs:
            lines.append(
                f"  • {svc['name']} — due {status['next_due']}"
            )
        lines.append("")

    upcoming_renewals = []
    all_inventory = conn.execute(
        "SELECT * FROM inventory"
    ).fetchall()

    for item in all_inventory:
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
        lines.append("❌ <b>OVERDUE Reminders:</b>")
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
    lines.append(f"  • Total: GHS {monthly_total:.2f}")
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
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
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
            item_type TEXT NOT NULL DEFAULT 'quantifiable',
            track_mode TEXT NOT NULL DEFAULT 'manual',
            current_stock REAL,
            regular_stock REAL,
            unit TEXT,
            renewal_date TEXT,
            last_service_date TEXT,
            frequency_days INTEGER,
            avg_cost REAL,
            avg_daily_use REAL,
            avg_daily_value REAL,
            avg_daily_value_unit TEXT,
            quick_deduct_1 REAL,
            quick_deduct_2 REAL,
            last_refill_date TEXT,
            last_refill_at TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Add new columns to existing database if upgrading
    new_columns = [
        ("item_type",        "TEXT DEFAULT 'quantifiable'"),
        ("track_mode",       "TEXT DEFAULT 'manual'"),
        ("last_service_date","TEXT"),
        ("frequency_days",   "INTEGER"),
        ("avg_cost",         "REAL"),
        ("avg_daily_use",    "REAL"),
        ("avg_daily_value",  "REAL"),
        ("avg_daily_value_unit", "TEXT"),
        ("quick_deduct_1",   "REAL"),
        ("quick_deduct_2",   "REAL"),
        ("last_refill_date", "TEXT"),
        ("last_refill_at",   "TEXT"),
    ]

    for col_name, col_def in new_columns:
        try:
            cursor.execute(
                f"ALTER TABLE inventory ADD COLUMN "
                f"{col_name} {col_def}"
            )
        except:
            pass

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usage_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER NOT NULL,
            amount_used REAL NOT NULL,
            log_date TEXT NOT NULL,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS service_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inventory_id INTEGER NOT NULL,
            service_date TEXT NOT NULL,
            cost REAL,
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
            recurrence TEXT,
            interval_minutes INTEGER,
            day_of_week INTEGER,
            day_of_month INTEGER,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            approved INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS alert_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sent_at TEXT NOT NULL
        )
    """)

    # Add new columns to existing reminders database if upgrading
    reminder_columns = [
        ("recurrence",       "TEXT"),
        ("interval_minutes", "INTEGER"),
        ("day_of_week",      "INTEGER"),
        ("day_of_month",     "INTEGER"),
    ]
    for col_name, col_def in reminder_columns:
        try:
            cursor.execute(
                f"ALTER TABLE reminders ADD COLUMN {col_name} {col_def}"
            )
        except Exception:
            pass

    user_columns = [
        ("approved", "INTEGER"),
    ]
    for col_name, col_def in user_columns:
        try:
            cursor.execute(
                f"ALTER TABLE users ADD COLUMN {col_name} {col_def}"
            )
        except Exception:
            pass

    # Grandfather every account that existed before `approved`
    # was introduced (approved IS NULL). New signups use 0 explicitly.
    try:
        cursor.execute(
            "UPDATE users SET approved = 1 WHERE approved IS NULL"
        )
    except Exception:
        pass

    conn.commit()

    restore_inventory_from_backup_if_empty(conn)
    migrate_electricity_bill_to_quantifiable(conn)
    migrate_microwave_service_to_expense(conn)
    ensure_default_inventory_items(conn)
    ensure_default_login_user(conn)
    backup_inventory(conn)

    conn.close()
    print("Database ready at:", DB_PATH)

def _safe_file_info(path_str):
    try:
        p = Path(path_str)
        if not p.exists():
            return {"exists": False}
        stat = p.stat()
        return {
            "exists": True,
            "size_bytes": stat.st_size,
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }
    except Exception as e:
        return {"exists": False, "error": str(e)}


def _inventory_row_to_dict(row):
    return {
        "name": row["name"],
        "category": row["category"],
        "item_type": row["item_type"] or "quantifiable",
        "track_mode": row["track_mode"] or "manual",
        "current_stock": row["current_stock"],
        "regular_stock": row["regular_stock"],
        "unit": row["unit"],
        "renewal_date": row["renewal_date"],
        "last_service_date": row["last_service_date"],
        "frequency_days": row["frequency_days"],
        "avg_cost": row["avg_cost"],
        "avg_daily_use": row["avg_daily_use"],
        "avg_daily_value": row["avg_daily_value"],
        "avg_daily_value_unit": row["avg_daily_value_unit"],
        "quick_deduct_1": row["quick_deduct_1"],
        "quick_deduct_2": row["quick_deduct_2"],
        "last_refill_date": row["last_refill_date"],
        "last_refill_at": row["last_refill_at"],
        "notes": row["notes"],
    }


def backup_inventory(conn=None):
    own_connection = conn is None
    if own_connection:
        conn = get_db()

    try:
        rows = conn.execute(
            "SELECT * FROM inventory ORDER BY name"
        ).fetchall()
        payload = {
            "exported_at": datetime.now().isoformat(),
            "item_count": len(rows),
            "items": [_inventory_row_to_dict(row) for row in rows],
        }
        os.makedirs(BACKUP_DIR, exist_ok=True)

        # Always keep a stable "latest" file.
        with open(INVENTORY_BACKUP_PATH, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Also keep timestamped snapshots for safety.
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        snap_path = os.path.join(BACKUP_DIR, f"inventory_backup-{ts}.json")
        with open(snap_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        # Prune old snapshots.
        try:
            snaps = []
            for name in os.listdir(BACKUP_DIR):
                if name.startswith("inventory_backup-") and name.endswith(".json"):
                    full = os.path.join(BACKUP_DIR, name)
                    try:
                        snaps.append((os.path.getmtime(full), full))
                    except Exception:
                        pass
            snaps.sort(reverse=True)
            for _, old_path in snaps[BACKUP_KEEP_COUNT:]:
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        except Exception:
            pass
    except Exception as e:
        print(f"Inventory backup failed: {e}")
    finally:
        if own_connection and conn is not None:
            conn.close()


def restore_inventory_from_backup_if_empty(conn):
    count = conn.execute(
        "SELECT COUNT(*) FROM inventory"
    ).fetchone()[0]

    if count > 0 or not os.path.exists(INVENTORY_BACKUP_PATH):
        return

    try:
        with open(INVENTORY_BACKUP_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)

        items = payload.get("items", [])
        for item in items:
            conn.execute("""
                INSERT INTO inventory
                (name, category, item_type, track_mode, current_stock,
                 regular_stock, unit, renewal_date, last_service_date,
                 frequency_days, avg_cost, avg_daily_use, quick_deduct_1,
                 quick_deduct_2, last_refill_date, last_refill_at, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                item.get("name"),
                item.get("category"),
                item.get("item_type", "quantifiable"),
                item.get("track_mode", "manual"),
                item.get("current_stock"),
                item.get("regular_stock"),
                item.get("unit"),
                item.get("renewal_date"),
                item.get("last_service_date"),
                item.get("frequency_days"),
                item.get("avg_cost"),
                item.get("avg_daily_use"),
                item.get("quick_deduct_1"),
                item.get("quick_deduct_2"),
                item.get("last_refill_date"),
                item.get("last_refill_at"),
                item.get("notes"),
            ))

        conn.commit()
        print(
            f"Restored {len(items)} inventory item(s) "
            f"from {INVENTORY_BACKUP_PATH}"
        )
    except Exception as e:
        print(f"Inventory restore failed: {e}")


def ensure_default_inventory_items(conn):
    try:
        existing_names = {
            row["name"].strip().lower()
            for row in conn.execute("SELECT name FROM inventory").fetchall()
            if row["name"]
        }

        inserted = 0
        for item in DEFAULT_INVENTORY_ITEMS:
            normalized_name = item["name"].strip().lower()
            if normalized_name in existing_names:
                continue

            if item["item_type"] == "service":
                conn.execute("""
                    INSERT INTO inventory
                    (name, category, item_type, track_mode, last_service_date,
                     frequency_days, avg_cost, notes)
                    VALUES (?, ?, 'service', 'manual', ?, ?, ?, ?)
                """, (
                    item["name"],
                    item["category"],
                    None,
                    int(item.get("frequency_days", 30)),
                    None,
                    "Seeded default item",
                ))
            else:
                conn.execute("""
                    INSERT INTO inventory
                    (name, category, item_type, track_mode, current_stock,
                     regular_stock, unit, renewal_date, avg_daily_use,
                     avg_daily_value, avg_daily_value_unit,
                     quick_deduct_1, quick_deduct_2, last_refill_date,
                     last_refill_at, notes)
                    VALUES (?, ?, 'quantifiable', 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    item["name"],
                    item["category"],
                    float(item.get("current_stock", 0)),
                    float(item.get("regular_stock", 0)),
                    item.get("unit", "unit"),
                    None,
                    None,
                    None,
                    None,
                    None,
                    None,
                    str(date.today()),
                    datetime.now().isoformat(),
                    "Seeded default item",
                ))

            existing_names.add(normalized_name)
            inserted += 1

        if inserted > 0:
            conn.commit()
            print(f"Inserted {inserted} default inventory item(s).")
    except Exception as e:
        print(f"Default inventory seed failed: {e}")


def migrate_microwave_service_to_expense(conn):
    """
    Microwave is tracked as an expense category item, not a recurring service row.
    """
    try:
        rows = conn.execute(
            """
            SELECT id FROM inventory
            WHERE LOWER(TRIM(name)) = 'microwave'
              AND item_type = 'service'
            """
        ).fetchall()
        if not rows:
            return

        dup = conn.execute(
            """
            SELECT 1 FROM expenses
            WHERE LOWER(TRIM(title)) = 'microwave'
            LIMIT 1
            """
        ).fetchone()

        deleted = 0
        for row in rows:
            inv_id = row["id"]
            conn.execute(
                "DELETE FROM service_log WHERE inventory_id = ?",
                (inv_id,),
            )
            conn.execute(
                "DELETE FROM usage_log WHERE inventory_id = ?",
                (inv_id,),
            )
            conn.execute("DELETE FROM inventory WHERE id = ?", (inv_id,))
            deleted += 1

        if deleted and not dup:
            conn.execute(
                """
                INSERT INTO expenses (title, amount, category, date, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "Microwave",
                    0.0,
                    "Maintenance",
                    str(date.today()),
                    (
                        "Added automatically: was a recurring service item; "
                        "record Microwave costs here as expenses.",
                    ),
                ),
            )

        if deleted:
            conn.commit()
            backup_inventory(conn)
            print(f"Microwave service row(s) removed; expense shell added if needed.")
    except Exception as e:
        print(f"Microwave migration failed: {e}")


def migrate_electricity_bill_to_quantifiable(conn):
    try:
        existing = conn.execute("""
            SELECT id, item_type, unit, current_stock, regular_stock
            FROM inventory
            WHERE LOWER(TRIM(name)) = 'electricity bill'
            ORDER BY id ASC
        """).fetchall()

        if not existing:
            return

        updated = 0
        for row in existing:
            if row["item_type"] == "quantifiable":
                continue

            conn.execute("""
                UPDATE inventory
                SET item_type = 'quantifiable',
                    track_mode = 'manual',
                    unit = COALESCE(NULLIF(unit, ''), 'kWh'),
                    current_stock = COALESCE(current_stock, 0),
                    regular_stock = COALESCE(regular_stock, 0),
                    last_service_date = NULL,
                    frequency_days = NULL
                WHERE id = ?
            """, (row["id"],))
            updated += 1

        if updated > 0:
            conn.commit()
            print(
                f"Migrated {updated} Electricity bill item(s) "
                "to quantifiable."
            )
    except Exception as e:
        print(f"Electricity bill migration failed: {e}")


def ensure_default_login_user(conn):
    try:
        existing = get_user_by_username(conn, LOGIN_USERNAME)
        if existing:
            return

        conn.execute("""
            INSERT INTO users (username, password_hash, approved)
            VALUES (?, ?, 1)
        """, (LOGIN_USERNAME, hash_password(LOGIN_PASSWORD)))
        conn.commit()
        print(f"Created default login user: {LOGIN_USERNAME}")
    except Exception as e:
        print(f"Default login user setup failed: {e}")


# ============================================================
# ALERT HELPERS
# ============================================================

def check_and_alert():
    conn      = get_db()
    inventory = conn.execute(
        "SELECT * FROM inventory WHERE item_type = 'quantifiable'"
    ).fetchall()
    low_stock_msgs = []

    for item in inventory:
        current = get_calculated_stock(item)
        if item["regular_stock"] and item["regular_stock"] > 0:
            pct = (current / item["regular_stock"]) * 100
            if pct <= 25:
                days_left = get_days_until_empty(
                    current, get_effective_daily_use(item)
                )
                msg = (
                    f"  • {item['name']}: {current}"
                    f" {item['unit']} ({int(pct)}% left)"
                )
                if days_left is not None:
                    msg += f" — {days_left} days left"
                low_stock_msgs.append(msg)

    services = conn.execute(
        "SELECT * FROM inventory WHERE item_type = 'service'"
    ).fetchall()

    service_msgs = []
    for svc in services:
        status = get_service_status(
            svc["last_service_date"], svc["frequency_days"]
        )
        if status["status"] == "overdue":
            service_msgs.append(
                f"  • {svc['name']} — "
                f"{status['days_overdue']} days overdue"
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

    if low_stock_msgs or overdue_msgs or service_msgs:
        lines = ["🏠 <b>Nku-OS Alert</b>"]

        if low_stock_msgs:
            lines.append("")
            lines.append("📦 <b>Low Stock:</b>")
            lines.extend(low_stock_msgs)

        if service_msgs:
            lines.append("")
            lines.append("🔧 <b>Overdue Services:</b>")
            lines.extend(service_msgs)

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
    info = None

    if request.args.get("pending") == "1":
        info = (
            "Registration received. Ask the administrator "
            f"({LOGIN_USERNAME}) to approve your account before you can sign in."
        )

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        conn = get_db()
        user = get_user_by_username(conn, username)
        conn.close()

        if user is not None:
            if user["password_hash"] != hash_password(password):
                error = "Wrong username or password. Try again."
            elif not user_is_fully_active(user):
                error = (
                    "This account has not been approved yet. "
                    "Please wait for the administrator."
                )
            else:
                session["logged_in"] = True
                session["username"]  = username

                send_telegram(
                    f"🔐 <b>Nku-OS Login</b>\n"
                    f"User <b>{username}</b> logged in\n"
                    f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
                )

                return redirect(url_for("home"))
        else:
            if username == LOGIN_USERNAME and password == LOGIN_PASSWORD:
                session["logged_in"] = True
                session["username"]  = username

                send_telegram(
                    f"🔐 <b>Nku-OS Login</b>\n"
                    f"User <b>{username}</b> logged in\n"
                    f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
                )

                return redirect(url_for("home"))
            error = "Wrong username or password. Try again."

    return render_template("login.html", error=error, info=info)


@app.route("/signup", methods=["POST"])
def signup():
    if is_logged_in():
        return redirect(url_for("home"))

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "").strip()
    password_confirm = request.form.get(
        "password_confirm", ""
    ).strip()

    if not username or not password:
        return render_template(
            "login.html",
            error="Username and password are required."
        )
    if len(username) < 3:
        return render_template(
            "login.html",
            error="Username must be at least 3 characters."
        )
    if len(password) < 6:
        return render_template(
            "login.html",
            error="Password must be at least 6 characters."
        )
    if password != password_confirm:
        return render_template(
            "login.html",
            error="Passwords do not match."
        )

    conn = get_db()
    existing = get_user_by_username(conn, username)
    if existing:
        conn.close()
        return render_template(
            "login.html",
            error="Username already exists. Choose another one."
        )

    conn.execute("""
        INSERT INTO users (username, password_hash, approved)
        VALUES (?, ?, 0)
    """, (username, hash_password(password)))
    conn.commit()
    conn.close()

    send_telegram(
        f"🆕 <b>New Nku-OS signup (pending approval)</b>\n"
        f"User <b>{username}</b> must be approved by admin\n"
        f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
    )

    return redirect(url_for("login", pending="1"))


@app.route("/logout")
def logout():
    username = session.get("username", "User")
    session.clear()

    send_telegram(
        f"🔓 <b>Nku-OS Logout</b>\n"
        f"User <b>{username}</b> logged out\n"
        f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
    )

    return redirect(url_for("login"))

@app.route("/admin/pending-users")
@primary_admin_required
def admin_pending_users():
    conn = get_db()
    try:
        pending = conn.execute(
            """
            SELECT id, username, created_at FROM users
            WHERE approved = 0
            ORDER BY datetime(COALESCE(created_at, '1970-01-01')) ASC,
                     id ASC
            """
        ).fetchall()
        total_users = conn.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]
        active_users = conn.execute(
            "SELECT COUNT(*) FROM users WHERE IFNULL(approved, 1) = 1"
        ).fetchone()[0]
    finally:
        conn.close()

    return render_template(
        "admin_accounts.html",
        pending=pending,
        total_users=total_users,
        active_users=active_users,
    )


@app.route("/admin/approve-user/<int:user_id>", methods=["POST"])
@primary_admin_required
def admin_approve_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, approved FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash("That account does not exist.", "warning")
        return redirect(url_for("admin_pending_users"))

    try:
        ap_val = (
            int(row["approved"]) if row["approved"] is not None else None
        )
    except (TypeError, ValueError):
        ap_val = None

    if ap_val == 1:
        conn.close()
        flash("That account was already approved.", "info")
        return redirect(url_for("admin_pending_users"))

    conn.execute(
        "UPDATE users SET approved = 1 WHERE id = ?",
        (user_id,),
    )
    conn.commit()
    conn.close()

    send_telegram(
        f"✅ <b>Nku-OS account approved</b>\n"
        f"User <b>{row['username']}</b> may sign in"
    )
    flash(
        f"You approved @{row['username']} — they can sign in now.",
        "success",
    )
    return redirect(url_for("admin_pending_users"))


@app.route("/admin/reject-user/<int:user_id>", methods=["POST"])
@primary_admin_required
def admin_reject_user(user_id):
    conn = get_db()
    row = conn.execute(
        "SELECT id, username, approved FROM users WHERE id = ?",
        (user_id,),
    ).fetchone()
    if not row:
        conn.close()
        flash("That account does not exist.", "warning")
        return redirect(url_for("admin_pending_users"))

    nu = LOGIN_USERNAME.strip().lower()
    if row["username"] and row["username"].strip().lower() == nu:
        conn.close()
        flash("You cannot reject the primary administrator account.", "danger")
        return redirect(url_for("admin_pending_users"))

    try:
        ap_val = (
            int(row["approved"]) if row["approved"] is not None else None
        )
    except (TypeError, ValueError):
        ap_val = None

    if ap_val == 1:
        conn.close()
        flash(
            "That user is already active. Reject only applies to pending requests.",
            "warning",
        )
        return redirect(url_for("admin_pending_users"))

    uname = row["username"]
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()

    send_telegram(
        f"❌ <b>Nku-OS signup rejected</b>\n"
        f"Removed pending user <b>{uname}</b>"
    )
    flash(f"Rejected and removed @{uname}.", "success")
    return redirect(url_for("admin_pending_users"))


@app.route("/debug/storage")
@login_required
def debug_storage():
    """
    Lightweight diagnostics page to confirm where data is stored.
    Useful when users think inventory/stocks disappeared after changes.
    """
    conn = get_db()
    try:
        inventory_count = conn.execute(
            "SELECT COUNT(*) FROM inventory"
        ).fetchone()[0]
        quant_count = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE item_type = 'quantifiable'"
        ).fetchone()[0]
        svc_count = conn.execute(
            "SELECT COUNT(*) FROM inventory WHERE item_type = 'service'"
        ).fetchone()[0]
        expenses_count = conn.execute(
            "SELECT COUNT(*) FROM expenses"
        ).fetchone()[0]
        reminders_count = conn.execute(
            "SELECT COUNT(*) FROM reminders"
        ).fetchone()[0]
    finally:
        conn.close()

    db_info = _safe_file_info(DB_PATH)
    backup_info = _safe_file_info(INVENTORY_BACKUP_PATH)

    restore_allowed = inventory_count == 0 and backup_info.get("exists") is True

    return f"""
    <div style="font-family:Segoe UI, Tahoma, sans-serif; padding:18px;">
      <h2 style="margin:0 0 10px;">Nku-OS Storage Debug</h2>
      <div style="color:#6b7280; margin-bottom:14px;">
        This page shows the <b>active</b> database path and backup file so you can confirm you’re looking at the right data.
      </div>

      <div style="background:#ffffff10; border:1px solid #2a2d3e; border-radius:12px; padding:14px; margin-bottom:14px;">
        <div><b>DB_PATH</b>: {DB_PATH}</div>
        <div style="margin-top:6px;"><b>database.db exists</b>: {db_info.get("exists")}</div>
        <div><b>database.db size</b>: {db_info.get("size_bytes","-")}</div>
        <div><b>database.db modified</b>: {db_info.get("modified_at","-")}</div>
      </div>

      <div style="background:#ffffff10; border:1px solid #2a2d3e; border-radius:12px; padding:14px; margin-bottom:14px;">
        <div><b>Backup path</b>: {INVENTORY_BACKUP_PATH}</div>
        <div style="margin-top:6px;"><b>inventory_backup.json exists</b>: {backup_info.get("exists")}</div>
        <div><b>backup size</b>: {backup_info.get("size_bytes","-")}</div>
        <div><b>backup modified</b>: {backup_info.get("modified_at","-")}</div>
      </div>

      <div style="background:#ffffff10; border:1px solid #2a2d3e; border-radius:12px; padding:14px; margin-bottom:14px;">
        <div style="margin-bottom:8px;"><b>Row counts</b></div>
        <div>Inventory (all): <b>{inventory_count}</b></div>
        <div>— Quantifiable: <b>{quant_count}</b></div>
        <div>— Services: <b>{svc_count}</b></div>
        <div>Expenses: <b>{expenses_count}</b></div>
        <div>Reminders: <b>{reminders_count}</b></div>
      </div>

      <div style="display:flex; gap:10px; flex-wrap:wrap;">
        <a href="/" style="padding:10px 14px; border-radius:10px; border:1px solid #2a2d3e; color:white; text-decoration:none;">← Back to Dashboard</a>
        {"<a href='/restore-from-backup' style='padding:10px 14px; border-radius:10px; border:1px solid #51cf66; color:#51cf66; text-decoration:none;'>Restore inventory from backup</a>" if restore_allowed else ""}
      </div>

      <div style="color:#6b7280; margin-top:14px; font-size:12px;">
        Restore button only appears when inventory is empty and a backup exists.
      </div>
    </div>
    """

@app.route("/restore-from-backup")
@login_required
def restore_from_backup():
    conn = get_db()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM inventory"
        ).fetchone()[0]
        if count > 0:
            return redirect(url_for("debug_storage"))
        restore_inventory_from_backup_if_empty(conn)
        ensure_default_inventory_items(conn)
        backup_inventory(conn)
        return redirect(url_for("home"))
    finally:
        conn.close()

IMPORT_TABLE_COLUMNS = {
    "inventory": (
        "name",
        "category",
        "item_type",
        "track_mode",
        "current_stock",
        "regular_stock",
        "unit",
        "renewal_date",
        "last_service_date",
        "frequency_days",
        "avg_cost",
        "avg_daily_use",
        "avg_daily_value",
        "avg_daily_value_unit",
        "quick_deduct_1",
        "quick_deduct_2",
        "last_refill_date",
        "last_refill_at",
        "notes",
        "created_at",
    ),
    "usage_log": (
        "inventory_id",
        "amount_used",
        "log_date",
        "notes",
        "created_at",
    ),
    "service_log": (
        "inventory_id",
        "service_date",
        "cost",
        "notes",
        "created_at",
    ),
    "expenses": (
        "title",
        "amount",
        "category",
        "date",
        "notes",
        "created_at",
    ),
    "reminders": (
        "title",
        "due_date",
        "priority",
        "done",
        "recurrence",
        "interval_minutes",
        "day_of_week",
        "day_of_month",
        "notes",
        "created_at",
    ),
}


def _insert_import_row(cursor, table, row_dict, column_tuple):
    vals = [row_dict.get(col) for col in column_tuple]
    cols_sql = ",".join(column_tuple)
    placeholders = ",".join(["?"] * len(column_tuple))
    cursor.execute(
        f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})",
        vals,
    )


def apply_full_json_import(conn, payload):
    """
    Replace inventory, usage_log, service_log, expenses, and reminders
    from an export payload. Does not modify users or login accounts.
    """
    if not isinstance(payload, dict):
        raise ValueError("Backup must be a JSON object at the top level.")

    inv = payload.get("inventory")
    exp = payload.get("expenses")
    rem = payload.get("reminders")
    if inv is None and exp is None and rem is None:
        raise ValueError(
            "Backup must include at least one of: "
            "inventory, expenses, reminders."
        )

    inv_list = inv if isinstance(inv, list) else []
    exp_list = exp if isinstance(exp, list) else []
    rem_list = rem if isinstance(rem, list) else []
    ulog_list = payload.get("usage_log")
    slog_list = payload.get("service_log")
    ulog_list = ulog_list if isinstance(ulog_list, list) else []
    slog_list = slog_list if isinstance(slog_list, list) else []

    cur = conn.cursor()
    cur.execute("DELETE FROM usage_log")
    cur.execute("DELETE FROM service_log")
    cur.execute("DELETE FROM inventory")
    cur.execute("DELETE FROM expenses")
    cur.execute("DELETE FROM reminders")

    id_map = {}
    icols = IMPORT_TABLE_COLUMNS["inventory"]

    for item in inv_list:
        if not isinstance(item, dict):
            continue
        raw_old = item.get("id")
        try:
            old_id = int(raw_old) if raw_old is not None else None
        except (TypeError, ValueError):
            old_id = None
        _insert_import_row(cur, "inventory", item, icols)
        new_id = cur.lastrowid
        if old_id is not None:
            id_map[old_id] = new_id

    use_cols = IMPORT_TABLE_COLUMNS["usage_log"]
    for log in ulog_list:
        if not isinstance(log, dict):
            continue
        try:
            oid = int(log.get("inventory_id"))
        except (TypeError, ValueError):
            continue
        nid = id_map.get(oid)
        if nid is None:
            continue
        row = dict(log)
        row["inventory_id"] = nid
        _insert_import_row(cur, "usage_log", row, use_cols)

    svc_cols = IMPORT_TABLE_COLUMNS["service_log"]
    for log in slog_list:
        if not isinstance(log, dict):
            continue
        try:
            oid = int(log.get("inventory_id"))
        except (TypeError, ValueError):
            continue
        nid = id_map.get(oid)
        if nid is None:
            continue
        row = dict(log)
        row["inventory_id"] = nid
        _insert_import_row(cur, "service_log", row, svc_cols)

    ecols = IMPORT_TABLE_COLUMNS["expenses"]
    for row in exp_list:
        if isinstance(row, dict):
            _insert_import_row(cur, "expenses", row, ecols)

    rcols = IMPORT_TABLE_COLUMNS["reminders"]
    for row in rem_list:
        if isinstance(row, dict):
            rr = dict(row)
            if rr.get("done") is None:
                rr["done"] = 0
            _insert_import_row(cur, "reminders", rr, rcols)

    return {
        "inventory": len(inv_list),
        "usage_log": len(ulog_list),
        "service_log": len(slog_list),
        "expenses": len(exp_list),
        "reminders": len(rem_list),
    }


@app.route("/export/json")
@login_required
def export_json():
    conn = get_db()
    try:
        inventory = conn.execute(
            "SELECT * FROM inventory ORDER BY name"
        ).fetchall()
        expenses = conn.execute(
            "SELECT * FROM expenses ORDER BY date DESC, id DESC"
        ).fetchall()
        reminders = conn.execute(
            "SELECT * FROM reminders ORDER BY due_date ASC, id ASC"
        ).fetchall()
        usage_log = conn.execute(
            "SELECT * FROM usage_log ORDER BY id ASC"
        ).fetchall()
        service_log = conn.execute(
            "SELECT * FROM service_log ORDER BY id ASC"
        ).fetchall()

        payload = {
            "export_version": 2,
            "exported_at": datetime.now().isoformat(),
            "db_path": DB_PATH,
            "inventory": [dict(row) for row in inventory],
            "usage_log": [dict(row) for row in usage_log],
            "service_log": [dict(row) for row in service_log],
            "expenses": [dict(row) for row in expenses],
            "reminders": [dict(row) for row in reminders],
        }
    finally:
        conn.close()

    from flask import Response
    body = json.dumps(payload, indent=2, default=str)
    filename = f"andyos-export-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )

@app.route("/export/csv/<string:table>")
@login_required
def export_csv(table):
    allowed = {"inventory", "expenses", "reminders"}
    if table not in allowed:
        return "Not allowed", 403

    conn = get_db()
    try:
        rows = conn.execute(
            f"SELECT * FROM {table} ORDER BY id ASC"
        ).fetchall()
        data = [dict(r) for r in rows]
    finally:
        conn.close()

    output = io.StringIO()
    writer = csv.writer(output)
    if not data:
        writer.writerow(["(no rows)"])
    else:
        headers = list(data[0].keys())
        writer.writerow(headers)
        for row in data:
            writer.writerow([row.get(h, "") for h in headers])

    from flask import Response
    filename = f"andyos-{table}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


IMPORT_JSON_MAX_BYTES = int(
    os.environ.get("ANDYOS_IMPORT_MAX_BYTES", str(25 * 1024 * 1024))
)


@app.route("/import/json", methods=["POST"])
@login_required
def import_json():
    if not request.form.get("confirm_replace"):
        flash(
            "Please check the confirmation box before restoring from backup.",
            "danger",
        )
        return redirect(url_for("settings"))

    raw_bytes = None
    up = request.files.get("json_file")
    if up and up.filename:
        raw_bytes = up.read()

    raw_text = ""
    if raw_bytes is not None:
        if len(raw_bytes) > IMPORT_JSON_MAX_BYTES:
            flash("Backup file is too large.", "danger")
            return redirect(url_for("settings"))
        raw_text = raw_bytes.decode("utf-8", errors="replace")

    if not raw_text.strip():
        raw_text = request.form.get("json_paste", "").strip()

    if len(raw_text.encode("utf-8")) > IMPORT_JSON_MAX_BYTES:
        flash("Backup JSON is too large.", "danger")
        return redirect(url_for("settings"))

    if not raw_text.strip():
        flash(
            "Paste the backup JSON or upload the file from "
            "\"Download full export (JSON)\".",
            "danger",
        )
        return redirect(url_for("settings"))

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        flash(f"Invalid JSON: {exc}", "danger")
        return redirect(url_for("settings"))

    conn = get_db()
    try:
        conn.execute("BEGIN IMMEDIATE")
        stats = apply_full_json_import(conn, payload)
        conn.commit()
        backup_inventory(conn)

        flash(
            "Restore complete — inventory "
            f"{stats['inventory']}, expenses {stats['expenses']}, "
            f"reminders {stats['reminders']}, usage logs "
            f"{stats['usage_log']}, service logs "
            f"{stats['service_log']}. User accounts were not changed.",
            "success",
        )

        send_telegram(
            f"📥 <b>Nku-OS full restore</b>\n"
            f"User <b>{session.get('username', '')}</b> imported a backup\n"
            f"📦 {stats['inventory']} inventory · "
            f"💰 {stats['expenses']} expenses · "
            f"🔔 {stats['reminders']} reminders\n"
            f"🕐 {datetime.now().strftime('%H:%M on %d %b %Y')}"
        )
    except ValueError as exc:
        conn.rollback()
        flash(str(exc), "danger")
    except sqlite3.Error as exc:
        conn.rollback()
        flash(f"Database error during restore: {exc}", "danger")
    finally:
        conn.close()

    return redirect(url_for("settings"))


@app.route("/settings")
@login_required
def settings():
    return render_template("settings.html")


# ============================================================
# ROUTES — DASHBOARD
# ============================================================

@app.route("/")
@login_required
def home():
    conn   = get_db()
    cursor = conn.cursor()

    inventory_raw = cursor.execute(
        "SELECT * FROM inventory WHERE item_type = 'quantifiable'"
        " ORDER BY name"
    ).fetchall()

    # Calculate current stock for each item
    inventory = []
    for item in inventory_raw:
        current = get_calculated_stock(item)
        days_left = get_days_until_empty(
            current, get_effective_daily_use(item)
        )
        pct = 0
        if item["regular_stock"] and item["regular_stock"] > 0:
            pct = min(
                int((current / item["regular_stock"]) * 100), 100
            )
        inventory.append({
            "item"     : item,
            "current"  : current,
            "days_left": days_left,
            "pct"      : pct
        })

    services_raw = cursor.execute(
        "SELECT * FROM inventory WHERE item_type = 'service'"
        " ORDER BY name"
    ).fetchall()

    services = []
    for svc in services_raw:
        status = get_service_status(
            svc["last_service_date"], svc["frequency_days"]
        )
        services.append((svc, status))

    expenses = cursor.execute(
        "SELECT * FROM expenses ORDER BY date DESC LIMIT 5"
    ).fetchall()

    reminders = cursor.execute(
        "SELECT * FROM reminders WHERE done = 0"
        " ORDER BY due_date ASC"
    ).fetchall()

    today       = date.today()
    month_start = f"{today.year}-{today.month:02d}-01"
    monthly_total = cursor.execute(
        "SELECT SUM(amount) FROM expenses WHERE date >= ?",
        (month_start,)
    ).fetchone()[0] or 0

    conn.close()

    low_stock = [
        i for i in inventory
        if i["pct"] <= 25
    ]

    overdue_services = [
        (svc, st) for svc, st in services
        if st["status"] == "overdue"
    ]

    today_str = str(date.today())
    overdue   = [r for r in reminders if r["due_date"] < today_str]

    if should_send_alert():
        check_and_alert()
        log_alert_sent()

    return render_template(
        "home.html",
        inventory=inventory,
        services=services,
        expenses=expenses,
        reminders=reminders,
        monthly_total=monthly_total,
        low_stock=low_stock,
        overdue_services=overdue_services,
        overdue=overdue,
        today=today_str
    )


# ============================================================
# ROUTES — QUICK DEDUCT / REFILL
# ============================================================

@app.route("/inventory/<int:item_id>")
@login_required
def inventory_detail(item_id):
    conn = get_db()
    try:
        item = conn.execute(
            "SELECT * FROM inventory WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return redirect(url_for("home"))

        if item["item_type"] != "quantifiable":
            return redirect(url_for("home"))

        current = get_calculated_stock(item)
        days_left = get_days_until_empty(current, get_effective_daily_use(item))
        pct = 0
        if item["regular_stock"] and item["regular_stock"] > 0:
            pct = min(int((current / item["regular_stock"]) * 100), 100)

        usage = conn.execute("""
            SELECT * FROM usage_log
            WHERE inventory_id = ?
            ORDER BY id DESC
            LIMIT 15
        """, (item_id,)).fetchall()
    finally:
        conn.close()

    return render_template(
        "inventory_detail.html",
        item=item,
        current=current,
        days_left=days_left,
        pct=pct,
        usage=usage,
    )

@app.route("/deduct/<int:item_id>", methods=["POST"])
@login_required
def deduct_stock(item_id):
    """Deduct a set amount from stock and log it."""
    amount = request.form.get("amount", 0)
    notes  = request.form.get("notes", "").strip()

    conn = get_db()
    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()

    if item:
        current = get_calculated_stock(item)
        new_stock = max(0, current - float(amount))

        conn.execute(
            "UPDATE inventory SET current_stock = ? WHERE id = ?",
            (new_stock, item_id)
        )

        # Log the usage
        conn.execute("""
            INSERT INTO usage_log
            (inventory_id, amount_used, log_date, notes)
            VALUES (?, ?, ?, ?)
        """, (
            item_id, float(amount),
            str(date.today()), notes or None
        ))

        conn.commit()
        backup_inventory(conn)

        # Check if just went below 25%
        if item["regular_stock"] and item["regular_stock"] > 0:
            new_pct = (new_stock / item["regular_stock"]) * 100
            old_pct = (current / item["regular_stock"]) * 100
            if new_pct <= 25 and old_pct > 25:
                send_telegram(
                    f"⚠️ <b>Stock Alert!</b>\n\n"
                    f"📦 <b>{item['name']}</b> just dropped "
                    f"below 25%\n"
                    f"Current: {new_stock} {item['unit']}\n"
                    f"That is {int(new_pct)}% of normal stock"
                )

    conn.close()
    ref = request.referrer or ""
    return redirect(ref if ("/inventory/" in ref) else url_for("home"))


@app.route("/refill/<int:item_id>", methods=["POST"])
@login_required
def refill_stock(item_id):
    """Mark item as refilled to full stock."""
    custom_amount = request.form.get("custom_amount", "").strip()

    conn = get_db()
    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()

    if item:
        new_stock = (
            float(custom_amount)
            if custom_amount
            else item["regular_stock"]
        )

        conn.execute("""
            UPDATE inventory
            SET current_stock = ?,
                last_refill_date = ?,
                last_refill_at = ?
            WHERE id = ?
        """, (
            new_stock,
            str(date.today()),
            datetime.now().isoformat(),
            item_id
        ))

        conn.commit()
        backup_inventory(conn)

        send_telegram(
            f"🛒 <b>Stock Refilled!</b>\n\n"
            f"📦 <b>{item['name']}</b>\n"
            f"Refilled to: {new_stock} {item['unit']}"
        )

    conn.close()
    ref = request.referrer or ""
    return redirect(ref if ("/inventory/" in ref) else url_for("home"))


# ============================================================
# ROUTES — ADD
# ============================================================

@app.route("/add", methods=["GET", "POST"])
@login_required
def add_item():
    if request.method == "POST":
        item_type  = request.form.get("item_type", "quantifiable")
        track_mode = request.form.get("track_mode", "manual")
        name       = request.form.get("name", "").strip()
        category   = request.form.get("category", "").strip()
        notes      = request.form.get("notes", "").strip()

        if not name or not category:
            return render_template(
                "add.html",
                error="Name and category are required.",
                section="inventory"
            )

        conn = get_db()

        if item_type == "quantifiable":
            current_stock  = request.form.get("current_stock", 0)
            regular_stock  = request.form.get("regular_stock", 0)
            unit           = request.form.get("unit", "").strip()
            renewal_date   = request.form.get(
                "renewal_date", ""
            ).strip()
            avg_daily_use  = request.form.get(
                "avg_daily_use", ""
            ).strip()
            avg_daily_value = request.form.get(
                "avg_daily_value", ""
            ).strip()
            avg_daily_value_unit = request.form.get(
                "avg_daily_value_unit", ""
            ).strip()
            quick_deduct_1 = request.form.get(
                "quick_deduct_1", ""
            ).strip()
            quick_deduct_2 = request.form.get(
                "quick_deduct_2", ""
            ).strip()

            conn.execute("""
                INSERT INTO inventory
                (name, category, item_type, track_mode,
                 current_stock, regular_stock, unit,
                 renewal_date, avg_daily_use,
                 avg_daily_value, avg_daily_value_unit,
                 quick_deduct_1, quick_deduct_2,
                 last_refill_date, last_refill_at, notes)
                VALUES (?, ?, 'quantifiable', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                name, category, track_mode,
                float(current_stock), float(regular_stock),
                unit, renewal_date or None,
                float(avg_daily_use) if avg_daily_use else None,
                float(avg_daily_value) if avg_daily_value else None,
                (avg_daily_value_unit or None),
                float(quick_deduct_1) if quick_deduct_1 else None,
                float(quick_deduct_2) if quick_deduct_2 else None,
                str(date.today()),
                datetime.now().isoformat(),
                notes or None
            ))

            send_telegram(
                f"✅ <b>New Inventory Item Added</b>\n\n"
                f"📦 <b>{name}</b>\n"
                f"Category: {category}\n"
                f"Stock: {current_stock} / {regular_stock} {unit}"
                + (f"\nAvg daily use: {avg_daily_use} {unit}"
                   if avg_daily_use else "")
            )

        else:  # service
            last_service_date = request.form.get(
                "last_service_date", ""
            ).strip()
            frequency_days = request.form.get("frequency_days", 30)
            avg_cost       = request.form.get("avg_cost", "").strip()

            conn.execute("""
                INSERT INTO inventory
                (name, category, item_type, last_service_date,
                 frequency_days, avg_cost, notes)
                VALUES (?, ?, 'service', ?, ?, ?, ?)
            """, (
                name, category,
                last_service_date or None,
                int(frequency_days),
                float(avg_cost) if avg_cost else None,
                notes or None
            ))

            send_telegram(
                f"🔧 <b>New Service Item Added</b>\n\n"
                f"⚙️ <b>{name}</b>\n"
                f"Category: {category}\n"
                f"Frequency: every {frequency_days} days"
            )

        conn.commit()
        backup_inventory(conn)
        conn.close()
        return redirect(url_for("home"))

    return render_template("add.html", section="inventory")


@app.route("/log-service/<int:item_id>", methods=["GET", "POST"])
@login_required
def log_service(item_id):
    conn = get_db()
    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()

    if not item:
        conn.close()
        return redirect(url_for("home"))

    if request.method == "POST":
        service_date = request.form.get(
            "service_date", str(date.today())
        ).strip()
        cost  = request.form.get("cost", "").strip()
        notes = request.form.get("notes", "").strip()

        conn.execute("""
            UPDATE inventory
            SET last_service_date = ?
            WHERE id = ?
        """, (service_date, item_id))

        conn.execute("""
            INSERT INTO service_log
            (inventory_id, service_date, cost, notes)
            VALUES (?, ?, ?, ?)
        """, (
            item_id, service_date,
            float(cost) if cost else None,
            notes or None
        ))

        if cost:
            conn.execute("""
                INSERT INTO expenses
                (title, amount, category, date, notes)
                VALUES (?, ?, ?, ?, ?)
            """, (
                f"{item['name']} service",
                float(cost),
                item["category"],
                service_date,
                notes or None
            ))

        conn.commit()
        backup_inventory(conn)
        conn.close()

        send_telegram(
            f"🔧 <b>Service Logged</b>\n\n"
            f"⚙️ <b>{item['name']}</b>\n"
            f"Date: {service_date}"
            + (f"\nCost: GHS {float(cost):.2f}" if cost else "")
        )

        return redirect(url_for("home"))

    history = conn.execute("""
        SELECT * FROM service_log
        WHERE inventory_id = ?
        ORDER BY service_date DESC
        LIMIT 10
    """, (item_id,)).fetchall()

    conn.close()

    return render_template(
        "log_service.html",
        item=item,
        history=history,
        today=str(date.today())
    )


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
            f"Amount: <b>GHS {float(amount):.2f}</b>\n"
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
        recurrence = request.form.get("recurrence", "none").strip()
        interval_minutes_raw = request.form.get("interval_minutes", "").strip()
        day_of_week_raw = request.form.get("day_of_week", "").strip()
        day_of_month_raw = request.form.get("day_of_month", "").strip()
        notes    = request.form.get("notes", "").strip()

        if not title or not due_date:
            return render_template(
                "add.html",
                error="Title and due date are required.",
                section="reminder"
            )

        interval_minutes = None
        day_of_week = None
        day_of_month = None
        if recurrence == "minutes":
            try:
                interval_minutes = int(interval_minutes_raw)
                if interval_minutes < 1:
                    interval_minutes = None
            except:
                interval_minutes = None
        if recurrence == "weekly":
            try:
                day_of_week = int(day_of_week_raw)
                if day_of_week < 0 or day_of_week > 6:
                    day_of_week = None
            except:
                day_of_week = None
        if recurrence == "monthly":
            try:
                day_of_month = int(day_of_month_raw)
                if day_of_month < 1 or day_of_month > 31:
                    day_of_month = None
            except:
                day_of_month = None

        conn = get_db()
        conn.execute("""
            INSERT INTO reminders
            (title, due_date, priority, recurrence,
             interval_minutes, day_of_week, day_of_month, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            title, due_date, priority,
            (recurrence if recurrence != "none" else None),
            interval_minutes, day_of_week, day_of_month,
            notes or None
        ))
        conn.commit()
        conn.close()

        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        emoji = priority_emoji.get(priority, "🔔")

        send_telegram(
            f"🔔 <b>New Reminder Set</b>\n\n"
            f"{emoji} <b>{title}</b>\n"
            f"Due: {due_date}\n"
            f"Priority: {priority.capitalize()}"
            + (f"\nRepeats: {recurrence}" if recurrence and recurrence != "none" else "")
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
    item = conn.execute(
        "SELECT * FROM inventory WHERE id = ?", (item_id,)
    ).fetchone()

    if not item:
        conn.close()
        return redirect(url_for("home"))

    if request.method == "POST":
        name     = request.form.get("name", "").strip()
        category = request.form.get("category", "").strip()
        notes    = request.form.get("notes", "").strip()
        expense_amount_raw = request.form.get(
            "expense_amount", ""
        ).strip()

        if item["item_type"] == "quantifiable":
            current_stock  = request.form.get("current_stock", 0)
            regular_stock  = request.form.get("regular_stock", 0)
            unit           = request.form.get("unit", "").strip()
            renewal_date   = request.form.get(
                "renewal_date", ""
            ).strip()
            avg_daily_use  = request.form.get(
                "avg_daily_use", ""
            ).strip()
            avg_daily_value = request.form.get(
                "avg_daily_value", ""
            ).strip()
            avg_daily_value_unit = request.form.get(
                "avg_daily_value_unit", ""
            ).strip()
            track_mode     = request.form.get(
                "track_mode", "manual"
            )
            quick_deduct_1 = request.form.get(
                "quick_deduct_1", ""
            ).strip()
            quick_deduct_2 = request.form.get(
                "quick_deduct_2", ""
            ).strip()

            conn.execute("""
                UPDATE inventory
                SET name=?, category=?, current_stock=?,
                    regular_stock=?, unit=?, renewal_date=?,
                    avg_daily_use=?, avg_daily_value=?, avg_daily_value_unit=?,
                    track_mode=?,
                    quick_deduct_1=?, quick_deduct_2=?,
                    notes=?
                WHERE id=?
            """, (
                name, category,
                float(current_stock), float(regular_stock),
                unit, renewal_date or None,
                float(avg_daily_use) if avg_daily_use else None,
                float(avg_daily_value) if avg_daily_value else None,
                (avg_daily_value_unit or None),
                track_mode,
                float(quick_deduct_1) if quick_deduct_1 else None,
                float(quick_deduct_2) if quick_deduct_2 else None,
                notes or None, item_id
            ))

        else:  # service
            last_service_date = request.form.get(
                "last_service_date", ""
            ).strip()
            frequency_days = request.form.get("frequency_days", 30)
            avg_cost       = request.form.get("avg_cost", "").strip()

            conn.execute("""
                UPDATE inventory
                SET name=?, category=?, last_service_date=?,
                    frequency_days=?, avg_cost=?, notes=?
                WHERE id=?
            """, (
                name, category,
                last_service_date or None,
                int(frequency_days),
                float(avg_cost) if avg_cost else None,
                notes or None, item_id
            ))

        if expense_amount_raw:
            try:
                expense_amount = float(expense_amount_raw)
                if expense_amount > 0:
                    conn.execute("""
                        INSERT INTO expenses
                        (title, amount, category, date, notes)
                        VALUES (?, ?, ?, ?, ?)
                    """, (
                        f"{name} expense",
                        expense_amount,
                        category,
                        str(date.today()),
                        "Auto-added from inventory edit",
                    ))
            except ValueError:
                pass

        conn.commit()
        backup_inventory(conn)
        conn.close()

        send_telegram(
            f"✏️ <b>Inventory Item Updated</b>\n\n"
            f"<b>{name}</b> has been updated"
        )

        return redirect(url_for("home"))

    conn.close()
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
            f"Amount: <b>GHS {float(amount):.2f}</b>"
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
        recurrence = request.form.get("recurrence", "none").strip()
        interval_minutes_raw = request.form.get("interval_minutes", "").strip()
        day_of_week_raw = request.form.get("day_of_week", "").strip()
        day_of_month_raw = request.form.get("day_of_month", "").strip()
        notes    = request.form.get("notes", "").strip()

        interval_minutes = None
        day_of_week = None
        day_of_month = None
        if recurrence == "minutes":
            try:
                interval_minutes = int(interval_minutes_raw)
                if interval_minutes < 1:
                    interval_minutes = None
            except:
                interval_minutes = None
        if recurrence == "weekly":
            try:
                day_of_week = int(day_of_week_raw)
                if day_of_week < 0 or day_of_week > 6:
                    day_of_week = None
            except:
                day_of_week = None
        if recurrence == "monthly":
            try:
                day_of_month = int(day_of_month_raw)
                if day_of_month < 1 or day_of_month > 31:
                    day_of_month = None
            except:
                day_of_month = None

        conn.execute("""
            UPDATE reminders
            SET title=?, due_date=?, priority=?, recurrence=?,
                interval_minutes=?, day_of_week=?, day_of_month=?, notes=?
            WHERE id=?
        """, (
            title, due_date, priority,
            (recurrence if recurrence != "none" else None),
            interval_minutes, day_of_week, day_of_month,
            notes or None, item_id
        ))
        conn.commit()
        conn.close()

        priority_emoji = {"high": "🔴", "medium": "🟡", "low": "🟢"}
        emoji = priority_emoji.get(priority, "🔔")

        send_telegram(
            f"✏️ <b>Reminder Updated</b>\n\n"
            f"{emoji} <b>{title}</b>\n"
            f"Due: {due_date}"
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
    if table == "inventory":
        backup_inventory(conn)
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
        "SELECT * FROM reminders WHERE id = ?", (item_id,)
    ).fetchone()

    def _compute_next_due(current_due, recurrence, interval_minutes, day_of_week, day_of_month):
        # due_date is stored as YYYY-MM-DD
        try:
            current = datetime.strptime(current_due, "%Y-%m-%d").date()
        except:
            current = date.today()

        today = date.today()

        if recurrence == "daily":
            return str(max(today, current) + timedelta(days=1))

        if recurrence == "weekly":
            # 0=Mon ... 6=Sun
            target = day_of_week if day_of_week is not None else 0
            base = max(today, current)
            delta = (target - base.weekday()) % 7
            if delta == 0:
                delta = 7
            return str(base + timedelta(days=delta))

        if recurrence == "monthly":
            target_day = day_of_month if day_of_month is not None else 1
            base = max(today, current)
            year = base.year
            month = base.month + 1
            if month == 13:
                month = 1
                year += 1
            # clamp to last day of month
            try:
                first_next = date(year, month, 1)
                last_day = (first_next + timedelta(days=32)).replace(day=1) - timedelta(days=1)
                d = min(target_day, last_day.day)
                return str(date(year, month, d))
            except:
                return str(base + timedelta(days=30))

        if recurrence == "minutes":
            # We still store due_date as date; so minutes recurrence will "roll" by setting due_date=today
            # and rely on external notification usage. Minimal support to keep it visible.
            return str(today)

        if recurrence == "yearly":
            base = max(today, current)
            try:
                return str(date(base.year + 1, base.month, base.day))
            except Exception:
                return str(base + timedelta(days=365))

        return None

    conn.execute(
        "UPDATE reminders SET done = 1 WHERE id = ?", (item_id,)
    )

    # If recurring, create a fresh next reminder (keeps history)
    if reminder and reminder["recurrence"]:
        next_due = _compute_next_due(
            reminder["due_date"],
            reminder["recurrence"],
            reminder["interval_minutes"],
            reminder["day_of_week"],
            reminder["day_of_month"],
        )
        if next_due:
            conn.execute("""
                INSERT INTO reminders
                (title, due_date, priority, done, recurrence,
                 interval_minutes, day_of_week, day_of_month, notes)
                VALUES (?, ?, ?, 0, ?, ?, ?, ?, ?)
            """, (
                reminder["title"],
                next_due,
                reminder["priority"],
                reminder["recurrence"],
                reminder["interval_minutes"],
                reminder["day_of_week"],
                reminder["day_of_month"],
                reminder["notes"],
            ))

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
    backup_inventory(conn)
    conn.close()

    return redirect(url_for("home"))


@app.route("/set-daily-average/<int:item_id>", methods=["POST"])
@login_required
def set_daily_average(item_id):
    avg_raw = request.form.get("avg_daily_use", "").strip()
    unit_raw = request.form.get("avg_daily_value_unit", "").strip()
    mode_raw = request.form.get("track_mode", "").strip() or "auto"

    try:
        avg_value = float(avg_raw) if avg_raw else None
    except ValueError:
        avg_value = None

    conn = get_db()
    conn.execute("""
        UPDATE inventory
        SET avg_daily_use = ?,
            avg_daily_value = ?,
            avg_daily_value_unit = ?,
            track_mode = ?
        WHERE id = ?
    """, (
        avg_value,
        avg_value,
        (unit_raw or None),
        mode_raw,
        item_id
    ))
    conn.commit()
    backup_inventory(conn)
    conn.close()
    return redirect(url_for("inventory_detail", item_id=item_id))


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

    inventory_raw = conn.execute(
        "SELECT * FROM inventory WHERE item_type = 'quantifiable'"
        " ORDER BY name"
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
        "SELECT COUNT(*) FROM reminders "
        "WHERE done=0 AND due_date < ?",
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

    stock_labels = [item["name"] for item in inventory_raw]
    stock_pct    = []
    for item in inventory_raw:
        current = get_calculated_stock(item)
        if item["regular_stock"] and item["regular_stock"] > 0:
            pct = (current / item["regular_stock"]) * 100
            stock_pct.append(round(min(pct, 100), 1))
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
        stock_current      = [],
        stock_regular      = [],
        stock_pct          = stock_pct,
        all_time_total     = all_time_total,
        total_transactions = total_transactions,
        monthly_this_month = monthly_this_month,
        avg_transaction    = avg_transaction,
        total_reminders    = total_reminders,
        done_reminders     = done_reminders,
        pending_reminders  = pending_reminders,
        overdue_reminders  = overdue_reminders,
        inventory_count    = len(inventory_raw),
    )


# ============================================================
# ROUTES — NOTIFICATION TEST + MANUAL BRIEFING
# ============================================================

@app.route("/test-whatsapp")
@login_required
def test_whatsapp():
    send_telegram(
        "🏠 <b>Nku-OS Test Message</b>\n\n"
        "✅ Your WhatsApp channel is connected and working!\n"
        f"🕐 Sent at {datetime.now().strftime('%H:%M on %d %b %Y')}"
    )
    return """
        <h2 style='font-family:sans-serif; color:#51cf66;'>
            ✅ Test message sent!
        </h2>
        <p style='font-family:sans-serif;'>Check your WhatsApp.</p>
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
            ✅ Daily briefing sent to WhatsApp!
        </h2>
        <p style='font-family:sans-serif;'>
            Check your WhatsApp to see the full briefing.
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