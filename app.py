"""Smart Parking Management System - Flask application."""
import csv
import io
import os
import secrets
from functools import wraps

from flask import (
    Flask, Response, abort, g, jsonify, redirect, render_template, request,
    session, url_for,
)
from werkzeug.security import check_password_hash

import database as db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_secret_key():
    """Use SECRET_KEY env var, else generate once and persist in instance/."""
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    path = os.path.join(BASE_DIR, "instance", "secret_key")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


def create_app(db_path=None, testing=False):
    app = Flask(__name__, instance_relative_config=True)
    app.config["SECRET_KEY"] = _load_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["DB_PATH"] = db_path or db.DB_PATH
    app.config["TESTING"] = testing

    db.init_db(app.config["DB_PATH"])

    # ------------------------------------------------------------ helpers
    def get_db():
        if "db" not in g:
            g.db = db.get_conn(app.config["DB_PATH"])
        return g.db

    @app.teardown_appcontext
    def close_db(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        return session["csrf"]

    app.jinja_env.globals["csrf_token"] = csrf_token

    def login_required(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if "operator_id" not in session:
                if request.path.startswith("/api/"):
                    return jsonify(ok=False, error="Please log in again."), 401
                return redirect(url_for("login", next=request.path))
            return view(*a, **kw)
        return wrapped

    def check_csrf():
        token = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token")
        if not token or token != session.get("csrf"):
            abort(400, "Invalid or missing CSRF token.")

    @app.errorhandler(db.ParkingError)
    def handle_parking_error(e):
        return jsonify(ok=False, error=e.message), e.status

    @app.errorhandler(400)
    def handle_400(e):
        if request.path.startswith("/api/"):
            return jsonify(ok=False, error=getattr(e, "description", "Bad request")), 400
        return e

    @app.after_request
    def no_cache(resp):
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------------------------------------------------- auth pages
    @app.route("/login", methods=["GET", "POST"])
    def login():
        if "operator_id" in session:
            return redirect(url_for("dashboard"))
        error = None
        if request.method == "POST":
            check_csrf()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            op = db.get_operator_by_username(get_db(), username)
            if op and check_password_hash(op["password_hash"], password):
                session.clear()
                session["operator_id"] = op["id"]
                session["username"] = op["username"]
                session.permanent = False
                csrf_token()
                db.log_activity(get_db(), op["id"], "LOGIN")
                nxt = request.args.get("next", "")
                if nxt.startswith("/") and not nxt.startswith("//"):
                    return redirect(nxt)
                return redirect(url_for("dashboard"))
            error = "Invalid username or password."
        return render_template("login.html", error=error)

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        check_csrf()
        db.log_activity(get_db(), session.get("operator_id"), "LOGOUT")
        session.clear()
        return redirect(url_for("login"))

    # --------------------------------------------------------------- pages
    @app.route("/")
    @login_required
    def dashboard():
        return render_template("dashboard.html", active="dashboard")

    @app.route("/layout")
    @login_required
    def parking_layout():
        return render_template("parking_layout.html", active="layout")

    @app.route("/entry")
    @login_required
    def vehicle_entry_page():
        return render_template("vehicle_entry.html", active="entry",
                               types=db.VEHICLE_TYPES)

    @app.route("/exit")
    @login_required
    def vehicle_exit_page():
        return render_template("vehicle_exit.html", active="exit")

    @app.route("/records")
    @login_required
    def vehicle_records_page():
        return render_template("vehicle_records.html", active="records",
                               types=db.VEHICLE_TYPES)

    # ----------------------------------------------------------------- API
    @app.get("/api/stats")
    @login_required
    def api_stats():
        conn = get_db()
        return jsonify(
            ok=True,
            stats=db.dashboard_stats(conn),
            hourly=db.hourly_entries_today(conn),
            recent=db.query_sessions(conn, limit=8),
        )

    @app.get("/api/slots")
    @login_required
    def api_slots():
        return jsonify(ok=True, slots=db.slots_with_vehicles(get_db()))

    @app.post("/api/entry")
    @login_required
    def api_entry():
        check_csrf()
        data = request.get_json(silent=True) or {}
        result = db.vehicle_entry(
            get_db(),
            session["operator_id"],
            data.get("vehicle_number"),
            data.get("vehicle_type"),
            owner_name=data.get("owner_name"),
            contact_number=data.get("contact_number"),
            charging_required=bool(data.get("charging_required")),
            fee_note=data.get("payment"),
        )
        return jsonify(ok=True, result=result,
                       stats=db.dashboard_stats(get_db())), 201

    @app.get("/api/exit/search")
    @login_required
    def api_exit_search():
        conn = get_db()
        row = db.find_active(conn, request.args.get("q", ""))
        if not row:
            raise db.ParkingError(
                "No vehicle currently inside matches that registration or slot.", 404)
        s = dict(row)
        s["type_label"] = db.VEHICLE_TYPES[s["vehicle_type"]]
        t_in = db.datetime.strptime(s["entry_time"], "%Y-%m-%d %H:%M:%S")
        mins = int((db.datetime.now() - t_in).total_seconds() // 60)
        s["duration_text"] = db.format_duration(mins)
        s["estimated_fee"] = db.calculate_fee(
            s["vehicle_type"], bool(s["charging_required"]), mins)
        return jsonify(ok=True, session=s)

    @app.post("/api/exit")
    @login_required
    def api_exit():
        check_csrf()
        data = request.get_json(silent=True) or {}
        try:
            sid = int(data.get("session_id"))
        except (TypeError, ValueError):
            raise db.ParkingError("Invalid parking session.")
        result = db.vehicle_exit(get_db(), session["operator_id"], sid)
        return jsonify(ok=True, result=result, stats=db.dashboard_stats(get_db()))

    def _filters():
        return dict(
            search=request.args.get("q") or None,
            vtype=request.args.get("type") or None,
            status=request.args.get("status") or None,
            date_from=request.args.get("from") or None,
            date_to=request.args.get("to") or None,
        )

    @app.get("/api/records")
    @login_required
    def api_records():
        rows = db.query_sessions(get_db(), **_filters())
        return jsonify(ok=True, records=rows, count=len(rows))

    @app.get("/records/export.csv")
    @login_required
    def export_csv():
        rows = db.query_sessions(get_db(), **_filters())
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["Registration", "Category", "Slot", "Owner", "Contact",
                    "EV Charging", "Entry Time", "Exit Time", "Status",
                    "Duration (min)", "Fee (Rs)"])
        for r in rows:
            def safe(v):  # neutralise spreadsheet formula injection
                v = "" if v is None else str(v)
                return "'" + v if v[:1] in ("=", "+", "-", "@") else v
            w.writerow([
                r["vehicle_number"], r["type_label"], r["slot_id"],
                safe(r["owner_name"]), safe(r["contact_number"]),
                "Yes" if r["charging_required"] else "No",
                r["entry_time"], r["exit_time"] or "", r["status"],
                r["live_minutes"] if r["live_minutes"] is not None else "",
                "" if r["fee"] is None else f"{r['fee']:.2f}",
            ])
        return Response(
            buf.getvalue(), mimetype="text/csv",
            headers={"Content-Disposition":
                     "attachment; filename=parking_records.csv"})

    @app.get("/api/summary")
    @login_required
    def api_summary():
        return jsonify(ok=True, stats=db.dashboard_stats(get_db()))

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
