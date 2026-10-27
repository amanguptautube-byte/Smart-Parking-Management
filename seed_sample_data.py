"""Optional: add sample parked vehicles so the dashboard has data to show.

Usage:  python seed_sample_data.py
Requires at least one operator (run create_operator.py first).
"""
import sys

import database as db


SAMPLES = [
    ("UP16AB1234", "CAR", "Rahul Sharma", "9876543210", False),
    ("UP14CD5678", "CAR", "Priya Singh", "9812345678", False),
    ("DL3CAF9090", "CAR", None, None, False),
    ("UP16EV0001", "EV", "Amit Verma", "9898989898", True),
    ("UP16EV0002", "EV", "Neha Gupta", None, False),
    ("UP16BK1111", "BIKE", "Karan", "9700000001", False),
    ("UP16BK2222", "BIKE", None, None, False),
]


def main():
    db.init_db()
    conn = db.get_conn()
    op = conn.execute("SELECT id FROM operators ORDER BY id LIMIT 1").fetchone()
    if not op:
        sys.exit("No operator found. Run create_operator.py first.")
    for reg, vt, name, phone, chg in SAMPLES:
        try:
            r = db.vehicle_entry(conn, op["id"], reg, vt, name, phone, chg)
            print(f"{reg} -> {r['slot_id']}")
        except db.ParkingError as e:
            print(f"{reg}: skipped ({e.message})")
    conn.close()


if __name__ == "__main__":
    main()
