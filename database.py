"""Database layer for the Smart Parking Management System (SQLite)."""
import math
import os
import re
import sqlite3
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get(
    "PARKING_DB", os.path.join(BASE_DIR, "instance", "parking.db")
)

# Slot configuration: (category, prefix, count)
SLOT_CONFIG = [
    ("CAR", "C", 150),
    ("EV", "E", 50),
    ("BIKE", "B", 50),
]

# Fee rates (Rs. per started hour). Set to 0 to disable fee calculation.
FEE_RATES = {"CAR": 40, "EV": 50, "BIKE": 15}
CHARGING_SURCHARGE_PER_HOUR = 30

VEHICLE_TYPES = {
    "CAR": "Petrol/Diesel Car",
    "EV": "Electric Car",
    "BIKE": "Bike",
}

REG_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9\- ]{3,14}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parking_slots (
    slot_id      TEXT PRIMARY KEY,
    category     TEXT NOT NULL CHECK (category IN ('CAR','EV','BIKE')),
    has_charger  INTEGER NOT NULL DEFAULT 0 CHECK (has_charger IN (0,1)),
    status       TEXT NOT NULL DEFAULT 'AVAILABLE'
                 CHECK (status IN ('AVAILABLE','OCCUPIED','INACTIVE'))
);

CREATE TABLE IF NOT EXISTS parking_sessions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    vehicle_number     TEXT NOT NULL,
    vehicle_type       TEXT NOT NULL CHECK (vehicle_type IN ('CAR','EV','BIKE')),
    slot_id            TEXT NOT NULL REFERENCES parking_slots(slot_id),
    owner_name         TEXT,
    contact_number     TEXT,
    charging_required  INTEGER NOT NULL DEFAULT 0 CHECK (charging_required IN (0,1)),
    entry_time         TEXT NOT NULL,
    exit_time          TEXT,
    status             TEXT NOT NULL DEFAULT 'IN' CHECK (status IN ('IN','OUT')),
    duration_minutes   INTEGER,
    fee                REAL,
    fee_paid_note      REAL,
    operator_id        INTEGER REFERENCES operators(id)
);

-- Database-level guarantees: one active session per vehicle, one per slot.
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_vehicle
    ON parking_sessions(vehicle_number) WHERE status = 'IN';
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_slot
    ON parking_sessions(slot_id) WHERE status = 'IN';
CREATE INDEX IF NOT EXISTS ix_sessions_entry ON parking_sessions(entry_time);

CREATE TABLE IF NOT EXISTS activity_logs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id    INTEGER REFERENCES operators(id),
    action         TEXT NOT NULL,
    vehicle_number TEXT,
    slot_id        TEXT,
    timestamp      TEXT NOT NULL
);
"""


class ParkingError(Exception):
    """Business-rule error that is safe to show to the operator."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_conn(db_path=None):
    path = db_path or DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(db_path=None):
    """Create tables and seed exactly 250 slots (idempotent)."""
    conn = get_conn(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.execute("BEGIN IMMEDIATE")
        for category, prefix, count in SLOT_CONFIG:
            rows = [
                (
                    f"{prefix}{i:03d}",
                    category,
                    1 if category == "EV" else 0,
                )
                for i in range(1, count + 1)
            ]
            # INSERT OR IGNORE => restart never duplicates slots.
            conn.executemany(
                "INSERT OR IGNORE INTO parking_slots(slot_id, category, has_charger) "
                "VALUES (?,?,?)",
                rows,
            )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ----------------------------------------------------------------- operators
def create_operator(conn, username, password_hash):
    conn.execute(
        "INSERT INTO operators(username, password_hash, created_at) VALUES (?,?,?)",
        (username, password_hash, now_str()),
    )


def get_operator_by_username(conn, username):
    return conn.execute(
        "SELECT * FROM operators WHERE username = ?", (username,)
    ).fetchone()


def log_activity(conn, operator_id, action, vehicle_number=None, slot_id=None):
    conn.execute(
        "INSERT INTO activity_logs(operator_id, action, vehicle_number, slot_id, timestamp) "
        "VALUES (?,?,?,?,?)",
        (operator_id, action, vehicle_number, slot_id, now_str()),
    )


# ---------------------------------------------------------------- validation
def normalize_reg(reg):
    return re.sub(r"\s+", " ", (reg or "").strip().upper())


def validate_registration(reg):
    reg = normalize_reg(reg)
    if not reg:
        raise ParkingError("Vehicle registration number is required.")
    if not REG_PATTERN.match(reg):
        raise ParkingError(
            "Invalid registration number. Use 4-15 letters/digits (e.g. UP16AB1234)."
        )
    return reg


# -------------------------------------------------------------- slot lookup
def _pick_slot(conn, vehicle_type, charging_required):
    """Choose the first free compatible slot. Must be called inside a write txn."""
    if vehicle_type == "CAR":
        category, charger_pref = "CAR", None
    elif vehicle_type == "BIKE":
        category, charger_pref = "BIKE", None
    else:
        category = "EV"
        charger_pref = True if charging_required else None

    base = (
        "SELECT slot_id FROM parking_slots WHERE category = ? AND status = 'AVAILABLE' "
        "AND slot_id NOT IN (SELECT slot_id FROM parking_sessions WHERE status='IN') "
    )
    if charger_pref:
        row = conn.execute(
            base + "AND has_charger = 1 ORDER BY slot_id LIMIT 1", (category,)
        ).fetchone()
    else:
        row = conn.execute(
            base + "ORDER BY slot_id LIMIT 1", (category,)
        ).fetchone()
    return row["slot_id"] if row else None


# ------------------------------------------------------------------- entry
def vehicle_entry(
    conn,
    operator_id,
    vehicle_number,
    vehicle_type,
    owner_name=None,
    contact_number=None,
    charging_required=False,
    fee_note=None,
):
    reg = validate_registration(vehicle_number)
    vehicle_type = (vehicle_type or "").upper()
    if vehicle_type not in VEHICLE_TYPES:
        raise ParkingError("Please select a valid vehicle type.")

    owner_name = (owner_name or "").strip()[:80] or None
    contact_number = (contact_number or "").strip() or None
    if contact_number and not re.fullmatch(r"\+?[0-9]{7,15}", contact_number):
        raise ParkingError("Contact number must contain 7-15 digits.")

    charging = 1 if (vehicle_type == "EV" and charging_required) else 0

    fee_val = None
    if fee_note not in (None, ""):
        try:
            fee_val = float(fee_note)
        except (TypeError, ValueError):
            raise ParkingError("Payment amount must be a number.")
        if fee_val < 0:
            raise ParkingError("Payment amount cannot be negative.")

    entered = now_str()
    try:
        conn.execute("BEGIN IMMEDIATE")  # serialises concurrent entries
        dup = conn.execute(
            "SELECT slot_id FROM parking_sessions WHERE vehicle_number=? AND status='IN'",
            (reg,),
        ).fetchone()
        if dup:
            raise ParkingError(
                f"Vehicle {reg} is already parked in slot {dup['slot_id']}.", 409
            )

        slot_id = _pick_slot(conn, vehicle_type, charging)
        if not slot_id:
            if vehicle_type == "EV" and charging:
                msg = "No EV charging slot is currently available."
            else:
                msg = f"No {VEHICLE_TYPES[vehicle_type]} slot is currently available."
            raise ParkingError(msg, 409)

        cur = conn.execute(
            "INSERT INTO parking_sessions(vehicle_number, vehicle_type, slot_id, owner_name, "
            "contact_number, charging_required, entry_time, status, fee_paid_note, operator_id) "
            "VALUES (?,?,?,?,?,?,?, 'IN', ?, ?)",
            (reg, vehicle_type, slot_id, owner_name, contact_number, charging,
             entered, fee_val, operator_id),
        )
        updated = conn.execute(
            "UPDATE parking_slots SET status='OCCUPIED' WHERE slot_id=? AND status='AVAILABLE'",
            (slot_id,),
        ).rowcount
        if updated != 1:
            raise ParkingError("Slot was taken by another operation. Please retry.", 409)
        log_activity(conn, operator_id, "ENTRY", reg, slot_id)
        conn.execute("COMMIT")
    except sqlite3.IntegrityError:
        _safe_rollback(conn)
        raise ParkingError(
            f"Vehicle {reg} is already parked or the slot was just taken.", 409
        )
    except Exception:
        _safe_rollback(conn)
        raise

    return {
        "session_id": cur.lastrowid,
        "vehicle_number": reg,
        "vehicle_type": vehicle_type,
        "vehicle_type_label": VEHICLE_TYPES[vehicle_type],
        "slot_id": slot_id,
        "entry_time": entered,
        "charging_required": bool(charging),
    }


def _safe_rollback(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.OperationalError:
        pass  # no active transaction


# -------------------------------------------------------------------- exit
def calculate_fee(vehicle_type, charging_required, minutes):
    rate = FEE_RATES.get(vehicle_type, 0)
    if rate <= 0:
        return 0.0
    hours = max(1, math.ceil(minutes / 60))
    fee = hours * rate
    if vehicle_type == "EV" and charging_required:
        fee += hours * CHARGING_SURCHARGE_PER_HOUR
    return float(fee)


def find_active(conn, query):
    q = normalize_reg(query)
    if not q:
        raise ParkingError("Enter a registration number or slot number.")
    return conn.execute(
        "SELECT * FROM parking_sessions WHERE status='IN' AND "
        "(vehicle_number = ? OR slot_id = ?)",
        (q, q),
    ).fetchone()


def vehicle_exit(conn, operator_id, session_id):
    exited = now_str()
    try:
        conn.execute("BEGIN IMMEDIATE")
        s = conn.execute(
            "SELECT * FROM parking_sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not s:
            raise ParkingError("Parking session not found.", 404)
        if s["status"] != "IN":
            raise ParkingError("This vehicle has already exited.", 409)

        t_in = datetime.strptime(s["entry_time"], "%Y-%m-%d %H:%M:%S")
        t_out = datetime.strptime(exited, "%Y-%m-%d %H:%M:%S")
        minutes = max(0, int((t_out - t_in).total_seconds() // 60))
        fee = calculate_fee(s["vehicle_type"], bool(s["charging_required"]), minutes)

        n = conn.execute(
            "UPDATE parking_sessions SET status='OUT', exit_time=?, duration_minutes=?, fee=? "
            "WHERE id=? AND status='IN'",
            (exited, minutes, fee, session_id),
        ).rowcount
        if n != 1:
            raise ParkingError("This vehicle has already exited.", 409)

        # Release slot only if no OTHER active session references it.
        conn.execute(
            "UPDATE parking_slots SET status='AVAILABLE' WHERE slot_id=? AND NOT EXISTS "
            "(SELECT 1 FROM parking_sessions WHERE slot_id=? AND status='IN')",
            (s["slot_id"], s["slot_id"]),
        )
        log_activity(conn, operator_id, "EXIT", s["vehicle_number"], s["slot_id"])
        conn.execute("COMMIT")
    except Exception:
        _safe_rollback(conn)
        raise

    return {
        "vehicle_number": s["vehicle_number"],
        "slot_id": s["slot_id"],
        "vehicle_type": s["vehicle_type"],
        "entry_time": s["entry_time"],
        "exit_time": exited,
        "duration_minutes": minutes,
        "duration_text": format_duration(minutes),
        "fee": fee,
    }


def format_duration(minutes):
    if minutes is None:
        return ""
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m}m" if h else f"{m}m"


# ---------------------------------------------------------------- reporting
def dashboard_stats(conn):
    def count(sql, *args):
        return conn.execute(sql, args).fetchone()[0]

    total = count("SELECT COUNT(*) FROM parking_slots")
    occupied = count("SELECT COUNT(*) FROM parking_slots WHERE status='OCCUPIED'")
    inactive = count("SELECT COUNT(*) FROM parking_slots WHERE status='INACTIVE'")
    by_cat = {}
    for cat, _, cap in SLOT_CONFIG:
        by_cat[cat] = {
            "capacity": cap,
            "occupied": count(
                "SELECT COUNT(*) FROM parking_slots WHERE category=? AND status='OCCUPIED'", cat
            ),
        }
    today = datetime.now().strftime("%Y-%m-%d")
    return {
        "total": total,
        "occupied": occupied,
        "available": total - occupied - inactive,
        "inactive": inactive,
        "by_category": by_cat,
        "inside": count("SELECT COUNT(*) FROM parking_sessions WHERE status='IN'"),
        "today_entries": count(
            "SELECT COUNT(*) FROM parking_sessions WHERE substr(entry_time,1,10)=?", today
        ),
        "today_exits": count(
            "SELECT COUNT(*) FROM parking_sessions WHERE substr(exit_time,1,10)=?", today
        ),
        "today_revenue": conn.execute(
            "SELECT COALESCE(SUM(fee),0) FROM parking_sessions WHERE substr(exit_time,1,10)=?",
            (today,),
        ).fetchone()[0],
        "ev_charging_now": count(
            "SELECT COUNT(*) FROM parking_sessions WHERE status='IN' AND charging_required=1"
        ),
    }


def slots_with_vehicles(conn):
    rows = conn.execute(
        "SELECT p.slot_id, p.category, p.has_charger, p.status, "
        "s.id AS session_id, s.vehicle_number, s.vehicle_type, s.owner_name, "
        "s.contact_number, s.entry_time, s.charging_required "
        "FROM parking_slots p LEFT JOIN parking_sessions s "
        "ON s.slot_id = p.slot_id AND s.status = 'IN' ORDER BY p.slot_id"
    ).fetchall()
    return [dict(r) for r in rows]


def query_sessions(conn, search=None, vtype=None, status=None,
                   date_from=None, date_to=None, limit=None):
    sql = "SELECT * FROM parking_sessions WHERE 1=1"
    args = []
    if search:
        sql += " AND (vehicle_number LIKE ? OR slot_id LIKE ? OR owner_name LIKE ?)"
        like = f"%{search.strip().upper()}%"
        args += [like, like, f"%{search.strip()}%"]
    if vtype in VEHICLE_TYPES:
        sql += " AND vehicle_type = ?"
        args.append(vtype)
    if status in ("IN", "OUT"):
        sql += " AND status = ?"
        args.append(status)
    if date_from:
        sql += " AND substr(entry_time,1,10) >= ?"
        args.append(date_from)
    if date_to:
        sql += " AND substr(entry_time,1,10) <= ?"
        args.append(date_to)
    sql += " ORDER BY entry_time DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        args.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    for r in rows:
        if r["status"] == "IN":
            t_in = datetime.strptime(r["entry_time"], "%Y-%m-%d %H:%M:%S")
            r["live_minutes"] = int((datetime.now() - t_in).total_seconds() // 60)
        else:
            r["live_minutes"] = r["duration_minutes"]
        r["duration_text"] = format_duration(r["live_minutes"])
        r["type_label"] = VEHICLE_TYPES.get(r["vehicle_type"], r["vehicle_type"])
    return rows


def hourly_entries_today(conn):
    today = datetime.now().strftime("%Y-%m-%d")
    counts = [0] * 24
    for r in conn.execute(
        "SELECT substr(entry_time,12,2) AS h, COUNT(*) AS c FROM parking_sessions "
        "WHERE substr(entry_time,1,10)=? GROUP BY h", (today,)
    ):
        counts[int(r["h"])] = r["c"]
    return counts
