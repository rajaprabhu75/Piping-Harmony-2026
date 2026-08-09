# Event Registration + QR Code Team Assignment (Flask)

Participants register on a web form → get a unique QR code emailed to them →
on event day, any organizer scans the QR code → the system auto-assigns the
participant to whichever of Team A/B/C/D currently has the fewest members →
future scans just display the already-assigned team → organizers can watch
live counts on a dashboard.

## 1. Install

```bash
cd event-qr-system
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## 2. Configure email

```bash
cp .env.example .env
```

Edit `.env` and fill in your SMTP details.

**If using Gmail:**
1. Turn on 2-Step Verification on the Google account: https://myaccount.google.com/security
2. Create an App Password: https://myaccount.google.com/apppasswords
3. Use that 16-character password as `MAIL_PASSWORD` (not your real Gmail password).

**If using another provider** (Outlook, a college/company SMTP server, SendGrid's
SMTP relay, etc.), just set `MAIL_SERVER`, `MAIL_PORT`, `MAIL_USE_TLS`/`MAIL_USE_SSL`,
`MAIL_USERNAME`, `MAIL_PASSWORD` accordingly.

## 3. Set BASE_URL correctly — this matters

The QR code encodes a URL: `BASE_URL/scan/<unique_code>`. Whatever device an
organizer uses to scan (phone camera, dedicated scanner app) needs to be able
to reach that URL.

- **Local testing on one laptop:** leave `BASE_URL=http://127.0.0.1:5000`.
- **Real event, phones on the same WiFi as your server:** set `BASE_URL` to
  your machine's LAN IP, e.g. `http://192.168.1.42:5000`, and run the server
  with `host='0.0.0.0'` (already set in `app.py`).
- **Real event, want a proper public URL:** deploy to a small VPS / Render /
  Railway / PythonAnywhere, or run `ngrok http 5000` and use the `https://...`
  URL ngrok gives you as `BASE_URL`.

Change `BASE_URL` in `.env` **before** participants register, since it's baked
into the QR code at registration time. If you must change it later, only
newly-registered participants get QR codes pointing to the new URL.

## 4. Run the server

```bash
python app.py
```

Server starts at `http://127.0.0.1:5000` (or `http://0.0.0.0:5000` — reachable
from other devices on the same network at your machine's LAN IP).

## 5. Pages overview

| Page | URL | Who uses it |
|---|---|---|
| Registration form | `/` | Participants |
| QR scan / team assignment | `/scan/<code>` | Opened automatically when a QR code is scanned with a phone camera |
| Webcam scan station | `/scan-station` | Organizers at a fixed desk — continuous scanning via laptop/tablet camera |
| Live team counts | `/dashboard` | Organizers, can be projected on a screen |
| Participant data (login required) | `/admin` | Organizers — full list of who registered, their team, timestamps |
| Raw counts JSON | `/counts` | For any custom dashboard/integration |

---

## 6. How organizers scan QR codes

There are two ways to scan — pick whichever suits your event:

**Option A — Just use a phone (simplest, no extra setup)**
Every QR code encodes a direct link (`/scan/<code>`). Any organizer can:
1. Open their phone's normal camera app (iPhone and most modern Android
   phones detect QR codes automatically) *or* any QR scanner app.
2. Point it at the participant's QR code (on their phone screen or printed).
3. Tap the notification/link that pops up — it opens the browser, shows the
   participant's name and their assigned team (or their existing team if
   already scanned before). No app installation needed.

**Option B — Dedicated "Scan Station" desk (best for a busy entrance)**
Open `/scan-station` on a laptop or tablet with a webcam, e.g.:
```
http://<server-ip>:5000/scan-station
```
- Allow camera access when the browser prompts.
- It scans continuously — just hold up each participant's QR code, one
  after another, without touching the screen or navigating between pages.
- Shows the participant's name and assigned team immediately after each scan.
- Good for setting up 4 such stations at once (one per team desk) or one
  shared station at the entrance.

Both options use the exact same underlying assignment logic — a participant
only ever gets assigned once, regardless of which method or how many times
they're scanned afterward.

---

## 7. How organizers view registration data

Go to `/admin`, e.g.:
```
http://<server-ip>:5000/admin
```
You'll be asked for the **admin password** (set via `ADMIN_PASSWORD` in
`.env` — change it from the default before your real event).

Once logged in you'll see:
- Total registered, and live count per team (A/B/C/D)
- A full table: name, email, phone, assigned team, registration time, scan
  time, and whether their confirmation email sent successfully
- Search by name/email, and filter by team (or "Not scanned yet" to see who
  hasn't been scanned)

Log out anytime via the "Log out" link on that page — this clears your admin
session on that browser.

**Security note:** `/admin` shows personal data (names, emails, phone
numbers), so it's password-protected. Only share the admin password with
your organizing team, and change `ADMIN_PASSWORD` from the default in `.env`
before the event. `/dashboard` and `/scan-station` don't require login since
they don't expose personal data beyond the participant currently being
scanned.

## How team assignment stays balanced

On each first-time scan, the app counts current members of Team A/B/C/D and
assigns the participant to whichever has the fewest (ties go to A, then B,
then C, then D). A lock prevents two simultaneous scans from both picking the
same "smallest" team and creating an imbalance.

**Note on scaling:** the lock used here (`threading.Lock`) only works
correctly if you run the Flask app as a **single process** (which
`python app.py` does by default). If you later deploy with a multi-process
WSGI server (e.g. multiple Gunicorn workers), replace this lock with a
database-level lock (e.g. `SELECT ... FOR UPDATE` in Postgres) since an
in-memory Python lock won't be shared across processes.

## Notes

- Each email address can register once (duplicate registrations are rejected
  and told to check their inbox).
- The SQLite database file `event.db` is created automatically on first run,
  in the project folder.
- To reset all data, stop the server and delete `event.db`.
