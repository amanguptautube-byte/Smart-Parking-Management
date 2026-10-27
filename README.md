# Smart Parking Management System

Web-based parking management for **250 slots** (150 cars `C001-C150`, 50 EVs `E001-E050`
with charging ports, 50 bikes `B001-B050`). Built with Flask, SQLite, HTML/CSS/JavaScript
and Chart.js. The operator enters vehicles manually; no sensors or cameras are needed.

## 1. Installation (Windows)

Install Python 3.10+ from python.org (tick **"Add Python to PATH"**), then open
**Command Prompt** or PowerShell:

```bat
cd path\to\smart-parking-system
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

In PowerShell, if activation is blocked, run once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

## 2. Create the operator account (demo setup)

No credentials are hardcoded. Create the first operator yourself:

```bat
python create_operator.py demo_operator
```

Enter a password of at least 8 characters when prompted (it is stored only as a salted hash).
Run the script again with another name to add more operators.

The database (`instance\parking.db`) and all 250 slots are created automatically the first
time the app or any script runs. Restarting never duplicates slots.

## 3. Optional sample data

```bat
python seed_sample_data.py
```

Parks 7 sample vehicles (cars, EVs, bikes) so the dashboard has data. Skip this if you
prefer to demonstrate from an empty lot.

## 4. Run the website

```bat
python app.py
```

Open **http://127.0.0.1:5000** and log in. Press `Ctrl+C` to stop. Data is kept in
`instance\parking.db` and survives restarts.

Optional: set a fixed session key with `set SECRET_KEY=your-long-random-string` before
starting. Otherwise one is generated once and saved in `instance\secret_key`.

## 5. Project structure

```
smart-parking-system/
  app.py                 Flask routes, authentication, JSON APIs, CSV export
  database.py            Schema, seeding, transactional entry/exit logic, fee rates
  create_operator.py     Creates operator accounts (hashed passwords)
  seed_sample_data.py    Optional demo vehicles
  test_parking.py        Automated tests (14 tests)
  requirements.txt
  templates/             base, login, dashboard, parking_layout,
                         vehicle_entry, vehicle_exit, vehicle_records
  static/css/style.css
  static/js/             common.js, dashboard.js, parking.js
  instance/              parking.db (created automatically)
```

## 6. How entry and exit work

**Entry** (`Vehicle Entry` page)
1. Operator fills registration number and vehicle type (owner, contact, payment optional;
   charging yes/no appears for electric cars). Entry time is recorded automatically.
2. The server validates the input, then opens a database transaction (`BEGIN IMMEDIATE`,
   so two simultaneous entries are processed one after another).
3. Rejects the entry if the same registration is already inside.
4. Picks the first free slot **of the matching category only** (car -> `C`, EV -> `E`,
   bike -> `B`). An EV needing charging gets a slot with a charging port; an EV not needing
   it gets any free EV slot.
5. Saves the session (status `IN`), marks the slot `OCCUPIED`, writes an activity log, commits.
6. If no compatible slot is free, a clear message is shown and nothing is saved.
7. The success panel shows vehicle number, slot, type and entry time; stats update.

**Exit** (`Vehicle Exit` page)
1. Operator searches by registration number or slot number (e.g. `UP16AB1234` or `C012`).
2. The vehicle's details, parked duration and estimated fee are shown.
3. **Mark Exit** asks for confirmation, then in one transaction: records the exit time,
   duration and fee, sets the session to `OUT`, and sets the slot to `AVAILABLE` only if no
   other active session uses it. The history row is kept permanently.
4. A receipt shows vehicle, slot, entry, exit, duration and fee.

Operators never edit slot availability by hand.

**Fee policy** (editable in `database.py`): Rs. 40/hour for cars, Rs. 50/hour for EVs,
Rs. 15/hour for bikes, plus Rs. 30/hour when charging is used. Charged per started hour,
minimum one hour. Set a rate to `0` to disable fees for that category.

**Slot colours:** green = available, red = occupied, blue = EV currently charging,
grey = inactive. An EV slot (with the lightning-bolt port icon) is green until a vehicle
actually occupies it. The "EV charging slots" filter on the layout page shows slots where an
EV is charging right now.

## 7. Running the automated tests

```bat
python -m unittest test_parking -v
```

Covers all 12 required cases (250 slots, per-category assignment, EV charger assignment,
exit releases slot, re-entry makes a new session, duplicate rejection, full category
rejection, 20 concurrent entries never share a slot, persistence across restart,
history retained) plus login, CSRF, CSV and validation checks. Tests use a temporary
database, so your real data is untouched.

## 8. Faculty demonstration checklist

- [ ] `python create_operator.py demo_operator`, then `python app.py`
- [ ] Open `/` without logging in: redirected to the login page
- [ ] Wrong password shows an error; correct login opens the dashboard
- [ ] Dashboard starts at 250 total / 0 occupied / 250 available
- [ ] Enter a **car**: slot `C001` assigned; occupied cars shows 1 / 150
- [ ] Enter an **EV with charging**: an `E` slot assigned; it appears blue on the layout
- [ ] Enter an **EV without charging**: an `E` slot assigned; it appears red
- [ ] Enter a **bike**: slot `B001` assigned
- [ ] Re-enter the same registration (any letter case): rejected as duplicate
- [ ] Submit an empty or invalid registration: validation message
- [ ] Parking Layout: all 250 slots, filters (All/Available/Occupied/Cars/EVs/Bikes/Charging), search by slot or vehicle, click a slot for vehicle details
- [ ] Vehicle Exit: search by registration, then by slot; confirm dialog; receipt with duration and fee
- [ ] After exit: slot is green again and dashboard counts drop
- [ ] Enter the same vehicle again: a new session is created
- [ ] Vehicle Records: search, type/status/date filters, history rows remain after exit
- [ ] Export CSV opens in Excel
- [ ] Stop the app (`Ctrl+C`), start again: all data still present
- [ ] Run `python -m unittest test_parking -v`: all tests pass
- [ ] Narrow the browser window to show the mobile layout and sidebar menu
- [ ] Log out; `/` redirects to login again

## 9. Security notes

Passwords are hashed (Werkzeug); sessions are server-signed cookies; all state-changing
requests require a CSRF token; SQL uses parameters; user text is HTML-escaped; CSV export
neutralises spreadsheet formulas. The app binds to `127.0.0.1` only. For real deployment use
a production server (e.g. waitress) behind HTTPS.
