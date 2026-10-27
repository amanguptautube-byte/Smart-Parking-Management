"""Automated tests for the 12 required cases + web smoke tests.

Run:  python -m unittest test_parking -v
"""
import os
import re
import tempfile
import threading
import unittest

from werkzeug.security import generate_password_hash

import database as db
from app import create_app


class ParkingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "test.db")
        db.init_db(self.path)
        self.conn = db.get_conn(self.path)
        db.create_operator(self.conn, "tester", generate_password_hash("password123"))
        self.op = self.conn.execute("SELECT id FROM operators").fetchone()["id"]

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def enter(self, reg, vt="CAR", **kw):
        return db.vehicle_entry(self.conn, self.op, reg, vt, **kw)

    def slot_status(self, slot):
        return self.conn.execute(
            "SELECT status FROM parking_slots WHERE slot_id=?", (slot,)).fetchone()[0]

    # 1
    def test_initial_250_slots_available(self):
        c = self.conn
        self.assertEqual(c.execute("SELECT COUNT(*) FROM parking_slots").fetchone()[0], 250)
        self.assertEqual(c.execute(
            "SELECT COUNT(*) FROM parking_slots WHERE status='AVAILABLE'").fetchone()[0], 250)
        for cat, n in (("CAR", 150), ("EV", 50), ("BIKE", 50)):
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM parking_slots WHERE category=?", (cat,)).fetchone()[0], n)
        db.init_db(self.path)  # restart must not duplicate
        self.assertEqual(c.execute("SELECT COUNT(*) FROM parking_slots").fetchone()[0], 250)

    # 2-4
    def test_each_category_gets_correct_slot(self):
        r = self.enter("UP16AB1234", "CAR")
        self.assertTrue(r["slot_id"].startswith("C"))
        self.assertEqual(r["slot_id"], "C001")
        self.assertEqual(self.slot_status("C001"), "OCCUPIED")
        r = self.enter("UP16EV0001", "EV")
        self.assertTrue(r["slot_id"].startswith("E"))
        r = self.enter("UP16BK0001", "BIKE")
        self.assertTrue(r["slot_id"].startswith("B"))
        self.assertEqual(db.dashboard_stats(self.conn)["occupied"], 3)

    # 5
    def test_ev_charging_gets_charger_slot(self):
        r = self.enter("UP16EV0002", "EV", charging_required=True)
        row = self.conn.execute(
            "SELECT has_charger FROM parking_slots WHERE slot_id=?", (r["slot_id"],)).fetchone()
        self.assertEqual(row[0], 1)
        self.assertTrue(r["charging_required"])

    # 6 + 11
    def test_exit_releases_slot_and_keeps_history(self):
        r = self.enter("UP16AB1234", "CAR")
        s = db.find_active(self.conn, "UP16AB1234")
        out = db.vehicle_exit(self.conn, self.op, s["id"])
        self.assertEqual(self.slot_status(r["slot_id"]), "AVAILABLE")
        self.assertEqual(out["slot_id"], r["slot_id"])
        self.assertGreaterEqual(out["fee"], 40)
        rows = db.query_sessions(self.conn, search="UP16AB1234")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "OUT")
        self.assertIsNotNone(rows[0]["exit_time"])
        with self.assertRaises(db.ParkingError):  # cannot exit twice
            db.vehicle_exit(self.conn, self.op, s["id"])

    # 7
    def test_reentry_creates_new_session(self):
        self.enter("UP16AB1234")
        s = db.find_active(self.conn, "UP16AB1234")
        db.vehicle_exit(self.conn, self.op, s["id"])
        self.enter("UP16AB1234")
        n = self.conn.execute(
            "SELECT COUNT(*) FROM parking_sessions WHERE vehicle_number='UP16AB1234'").fetchone()[0]
        self.assertEqual(n, 2)

    # 8
    def test_duplicate_active_entry_rejected(self):
        self.enter("UP16AB1234")
        with self.assertRaises(db.ParkingError) as ctx:
            self.enter("up16ab1234")  # case-insensitive
        self.assertEqual(ctx.exception.status, 409)
        self.assertEqual(db.dashboard_stats(self.conn)["inside"], 1)

    # 9
    def test_full_category_rejects(self):
        for i in range(50):
            self.enter(f"BK{i:04d}", "BIKE")
        with self.assertRaises(db.ParkingError) as ctx:
            self.enter("BKEXTRA1", "BIKE")
        self.assertIn("available", ctx.exception.message)
        self.assertEqual(db.dashboard_stats(self.conn)["by_category"]["BIKE"]["occupied"], 50)
        # cars unaffected
        self.assertTrue(self.enter("UP16AB1234", "CAR")["slot_id"].startswith("C"))
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM parking_sessions").fetchone()[0], 51)

    # 10
    def test_concurrent_entries_get_distinct_slots(self):
        results, errors = [], []

        def worker(i):
            c = db.get_conn(self.path)
            try:
                results.append(db.vehicle_entry(c, self.op, f"UP16CC{i:04d}", "CAR")["slot_id"])
            except Exception as e:  # noqa
                errors.append(e)
            finally:
                c.close()

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 20)
        self.assertEqual(len(set(results)), 20)

    # 12
    def test_data_persists_across_restart(self):
        self.enter("UP16AB1234")
        self.conn.close()
        db.init_db(self.path)  # simulated restart
        self.conn = db.get_conn(self.path)
        self.assertEqual(db.dashboard_stats(self.conn)["inside"], 1)
        self.assertEqual(self.slot_status("C001"), "OCCUPIED")

    def test_validation(self):
        with self.assertRaises(db.ParkingError):
            self.enter("")
        with self.assertRaises(db.ParkingError):
            self.enter("??")
        with self.assertRaises(db.ParkingError):
            self.enter("UP16AB1234", "TRUCK")
        with self.assertRaises(db.ParkingError):
            self.enter("UP16AB1234", "CAR", contact_number="abc")
        self.assertEqual(db.dashboard_stats(self.conn)["inside"], 0)

    def test_db_blocks_double_booking_of_slot(self):
        self.enter("UP16AB1234")
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO parking_sessions(vehicle_number,vehicle_type,slot_id,entry_time,status) "
                "VALUES ('OTHER1234','CAR','C001','2026-01-01 00:00:00','IN')")


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = os.path.join(self.tmp.name, "web.db")
        self.app = create_app(db_path=path, testing=True)
        c = db.get_conn(path)
        db.create_operator(c, "tester", generate_password_hash("password123"))
        c.close()
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def login(self):
        page = self.client.get("/login").get_data(as_text=True)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        return self.client.post("/login", data={
            "username": "tester", "password": "password123", "csrf_token": tok})

    def csrf(self):
        page = self.client.get("/").get_data(as_text=True)
        return re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)

    def test_protected_pages_redirect(self):
        for p in ("/", "/layout", "/entry", "/exit", "/records", "/api/stats"):
            r = self.client.get(p)
            self.assertIn(r.status_code, (302, 401), p)

    def test_bad_login(self):
        page = self.client.get("/login").get_data(as_text=True)
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        r = self.client.post("/login", data={"username": "tester", "password": "nope",
                                             "csrf_token": tok})
        self.assertIn(b"Invalid username or password", r.data)

    def test_full_flow(self):
        self.assertEqual(self.login().status_code, 302)
        for p in ("/", "/layout", "/entry", "/exit", "/records"):
            self.assertEqual(self.client.get(p).status_code, 200, p)
        tok = self.csrf()
        h = {"X-CSRF-Token": tok}

        r = self.client.post("/api/entry", json={"vehicle_number": "UP16AB1234", "vehicle_type": "CAR"})
        self.assertEqual(r.status_code, 400)  # missing CSRF

        r = self.client.post("/api/entry", headers=h, json={
            "vehicle_number": "UP16AB1234", "vehicle_type": "CAR", "owner_name": "=cmd"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.get_json()["result"]["slot_id"], "C001")
        r = self.client.post("/api/entry", headers=h, json={
            "vehicle_number": "UP16AB1234", "vehicle_type": "CAR"})
        self.assertEqual(r.status_code, 409)

        stats = self.client.get("/api/stats").get_json()["stats"]
        self.assertEqual((stats["occupied"], stats["available"], stats["total"]), (1, 249, 250))
        slots = self.client.get("/api/slots").get_json()["slots"]
        self.assertEqual(len(slots), 250)

        s = self.client.get("/api/exit/search?q=C001").get_json()["session"]
        self.assertEqual(s["vehicle_number"], "UP16AB1234")
        r = self.client.post("/api/exit", headers=h, json={"session_id": s["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/exit/search?q=C001").status_code, 404)

        csv_text = self.client.get("/records/export.csv").get_data(as_text=True)
        self.assertIn("UP16AB1234", csv_text)
        self.assertIn("'=cmd", csv_text)  # formula injection neutralised

        self.client.post("/logout", data={"csrf_token": tok})
        self.assertEqual(self.client.get("/").status_code, 302)


if __name__ == "__main__":
    unittest.main()
