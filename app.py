import os
import io
import uuid
import time
import random
import threading
from datetime import datetime

from functools import wraps
from flask import Flask, request, redirect, url_for, render_template, jsonify, flash, session, send_file
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
from sqlalchemy.pool import NullPool
import qrcode
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///event.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# NullPool = no connection reuse. Without this, an already-open SQLite
# connection can keep reading the OLD file's data after /admin/upload-db
# replaces event.db on disk (the OS lets the old open handle keep pointing
# at the old inode). Opening fresh every query guarantees we always see
# whatever is actually on disk right now.
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}


# ---------------- Mail configuration (set these in .env) ----------------

app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get(
    'MAIL_DEFAULT_SENDER',
    os.environ.get('MAIL_USERNAME')
)


# ---------------- Event details (edit or set via .env) ----------------

EVENT_NAME = os.environ.get('EVENT_NAME', 'Piping harmony 2026')
EVENT_DATE = os.environ.get('EVENT_DATE', 'September 05, 2026')
EVENT_VENUE = os.environ.get(
    'EVENT_VENUE',
    'Intercontinental,Chennai Mahabalipuram Road, Chennai, Tamil Nadu 600100'
)

# Google Maps location is used in the confirmation email only.
EVENT_MAP_URL = os.environ.get(
    'EVENT_MAP_URL',
    'https://www.google.com/maps/search/?api=1&query=InterContinental+Chennai+Mahabalipuram+Road+Chennai+Tamil+Nadu+600100'
)

# QR background/template image. Place the supplied Piping Harmony image at
# static/piping_harmony_qr_background.png, or override this with the
# QR_BACKGROUND_IMAGE environment variable.
QR_BACKGROUND_IMAGE = os.environ.get(
    'QR_BACKGROUND_IMAGE',
    os.path.join('static', 'piping_harmony_qr_background.png')
)

# BASE_URL must be reachable by whatever device/app the organizers use to scan.
# For local testing http://127.0.0.1:5000 is fine. For a real event, deploy the
# app (e.g. on a small VPS or ngrok tunnel) and put that public URL here.
BASE_URL = os.environ.get('BASE_URL', 'http://127.0.0.1:5000')


# Password organizers use to view the participant data (/admin).
# Change this in .env — do not leave the default for a real event.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')


# Email domains allowed to self-register. A tuple works directly with
# str.endswith(), and keeping it here (next to the other event config)
# means adding another allowed domain later is a one-line change instead
# of hunting through the register() view.
ALLOWED_EMAIL_DOMAINS = ('@ten.com', '@external.ten.com')


# ---------------- TEAM CONFIGURATION ----------------
#
# Six teams for Piping Harmony 2026.
#
TEAMS = [
    'Bolt Billa',
    'Elbow Ekambaram',
    'Jacketed Jackson',
    'Nozzle Nesamani',
    'Reducer Reeta',
    'Valve Vallaiyapathi'
]


db = SQLAlchemy(app)
mail = Mail(app)


# Simple in-process lock so two simultaneous scans can't both grab the same
# "smallest" team. Fine for a single-process dev server. See README for notes
# on scaling this to multiple workers / a real production deployment.
assign_lock = threading.Lock()


class Participant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    emp_id = db.Column(db.String(30), nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=False, unique=True)
    phone = db.Column(db.String(20))
    unique_code = db.Column(db.String(36), unique=True, nullable=False)

    # Changed from String(1) because team names are now full names.
    team = db.Column(db.String(50), nullable=True)

    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    scanned_at = db.Column(db.DateTime, nullable=True)
    email_sent = db.Column(db.Boolean, default=False)

    # Event-day logistics questions asked at registration.
    needs_bus = db.Column(db.Boolean, default=False)
    needs_breakfast = db.Column(db.Boolean, default=False)
    needs_beer = db.Column(db.Boolean, default=False)
    needs_food = db.Column(db.Boolean, default=False)

    # Only meaningful when needs_food is True. Values: 'veg' / 'nonveg'.
    food_preference = db.Column(db.String(10), nullable=True)

    # The same QR code is used at two separate scan stations: the team-entry
    # station (team / scanned_at above) and the beer counter (below). The two
    # are tracked independently so scanning at one has no effect on the other.
    beer_received = db.Column(db.Boolean, default=False)
    beer_scanned_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
        return {
            'id': self.id,
            'emp_id': self.emp_id,
            'name': self.name,
            'email': self.email,
            'unique_code': self.unique_code,
            'team': self.team,
            'registered_at': self.registered_at.isoformat()
                if self.registered_at else None,
            'scanned_at': self.scanned_at.isoformat()
                if self.scanned_at else None,
            'needs_bus': self.needs_bus,
            'needs_breakfast': self.needs_breakfast,
            'needs_beer': self.needs_beer,
            'needs_food': self.needs_food,
            'food_preference': self.food_preference,
            'beer_received': self.beer_received,
            'beer_scanned_at': self.beer_scanned_at.isoformat()
                if self.beer_scanned_at else None,
        }


with app.app_context():
    db.create_all()

    # Lightweight migration: if an existing event.db was created before the
    # emp_id column existed, add it in place instead of requiring a wipe.
    with db.engine.connect() as conn:
        existing_cols = [
            row[1]
            for row in conn.exec_driver_sql(
                "PRAGMA table_info(participant)"
            )
        ]

        if 'emp_id' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN emp_id VARCHAR(30)"
            )

        # New columns for the bus / beer questions and the separate
        # beer-counter scan. Existing rows default to "no" / "not received".
        if 'needs_bus' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN needs_bus BOOLEAN DEFAULT 0"
            )

        if 'needs_breakfast' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN needs_breakfast BOOLEAN DEFAULT 0"
            )

        if 'needs_beer' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN needs_beer BOOLEAN DEFAULT 0"
            )

        if 'beer_received' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN beer_received BOOLEAN DEFAULT 0"
            )

        if 'beer_scanned_at' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN beer_scanned_at DATETIME"
            )

        # New columns for the food question and veg/non-veg preference.
        # Existing rows default to "no food" / no preference set.
        if 'needs_food' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN needs_food BOOLEAN DEFAULT 0"
            )

        if 'food_preference' not in existing_cols:
            conn.exec_driver_sql(
                "ALTER TABLE participant ADD COLUMN food_preference VARCHAR(10)"
            )


def safe_filename_part(text: str) -> str:
    """Turns a participant's name/emp_id into a filesystem-safe chunk for
    use in a downloaded filename — strips anything that isn't a letter,
    digit, space, hyphen, or underscore, then swaps spaces for underscores."""

    cleaned = ''.join(
        c for c in text
        if c.isalnum() or c in (' ', '-', '_')
    ).strip()

    return cleaned.replace(' ', '_') or 'participant'


def generate_qr_image(
    data: str,
    caption_top: str = None,
    caption_bottom: str = None
) -> io.BytesIO:
    """Generate the participant QR on the Piping Harmony background image.

    The supplied 1024x1536 portrait template already contains the event artwork
    and the white QR frame below "SCAN ME". The QR is centered inside that frame.

    caption_top and caption_bottom are retained for compatibility with the
    existing calls, but the supplied background already contains the event
    artwork, so the captions are intentionally not drawn over it.
    """

    from PIL import Image

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=30,
        border=1,
    )

    qr.add_data(data)
    qr.make(fit=True)

    qr_img = qr.make_image(
        fill_color="black",
        back_color="white"
    ).convert("RGB")

    # Resolve the background relative to the project directory so the app
    # works even when it is launched from a different current working folder.
    background_path = QR_BACKGROUND_IMAGE
    if not os.path.isabs(background_path):
        background_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            background_path
        )

    if not os.path.exists(background_path):
        raise FileNotFoundError(
            f"QR background image not found: {background_path}. "
            "Place the Piping Harmony background image at "
            "static/piping_harmony_qr_background.png or set "
            "QR_BACKGROUND_IMAGE to its correct path."
        )

    background = Image.open(background_path).convert("RGB")

    # The supplied template is 1024x1536. Keep the original artwork size.
    # The white QR frame is approximately x=300..705 and y=705..1305.
    # A 360x360 QR gives a good scanning margin without covering the frame.
    qr_size = min(
        205,
        background.width - 80,
        background.height - 150
    )

    # Resize with NEAREST so QR module edges remain crisp and easy to scan.
    qr_img = qr_img.resize(
        (qr_size, qr_size),
        Image.Resampling.NEAREST
    )

    # Center horizontally and place it inside the white frame below SCAN ME.
    x = (background.width - qr_size) // 2
    y = 410

    # Keep the QR inside the background even if the template is changed later.
    y = max(0, min(y, background.height - qr_size))

    background.paste(qr_img, (x, y))

    buf = io.BytesIO()
    background.save(buf, format='PNG', optimize=True)
    buf.seek(0)

    return buf


def send_qr_email(participant: 'Participant') -> bool:

    """Emails the participant a QR code that encodes the /scan/<code> URL,
    so any organizer's phone camera / QR app can open it directly."""

    scan_url = f"{BASE_URL}/scan/{participant.unique_code}"

    qr_buf = generate_qr_image(
        scan_url,
        caption_top=EVENT_NAME,
        caption_bottom=participant.name
    )

    subject = f"Your QR Code for {EVENT_NAME}"

    google_map_url = EVENT_MAP_URL

    html_body = f"""
    <html>
      <body>
        <p>Hi {participant.name},</p>

        <p>Thank you for registering for <b>{EVENT_NAME}</b>!</p>

        <p><b>Employee ID:</b> {participant.emp_id or '-'}</p>

        <p>
          <b>Event Details:</b><br>
          <b>Date:</b> {EVENT_DATE}<br>
          <b>Venue:</b> {EVENT_VENUE}<br>
          <a href="{google_map_url}">View Location on Google Maps</a>
        </p>

        <p>
          Your <b>unique QR code</b> is attached to this email. Please bring it
          (on your phone or printed) on event day. Any organizer can scan it
          and you'll be automatically assigned to a team.
        </p>

        <p><b>Your registration code:</b> {participant.id}</p>

        <p>See you there!</p>
        <p>Piping Harmony 2026 – Organizing Committee</p>
      </body>
    </html>
    """

    plain_body = f"""Hi {participant.name},

Thank you for registering for {EVENT_NAME}!

Employee ID: {participant.emp_id or '-'}

Event Details:
Date: {EVENT_DATE}
Venue: {EVENT_VENUE}
Location: {google_map_url}

Your unique QR code is attached to this email. Please bring it on event day.
Any organizer can scan it and you will be automatically assigned to a team.

Your registration code: {participant.id}

See you there!
"""

    msg = Message(
        subject=subject,
        recipients=[participant.email],
        body=plain_body,
        html=html_body
    )

    msg.attach(
        'event_qr_code.png',
        'image/png',
        qr_buf.read()
    )

    try:
        mail.send(msg)
        return True

    except Exception as e:
        app.logger.error(
            f"Failed to send email to {participant.email}: {e}"
        )
        return False


def send_qr_email_async(participant_id: int) -> None:
    """Runs send_qr_email() in a background thread so the /register request
    doesn't have to wait for the Gmail SMTP round-trip (usually 1-3s, but can
    spike much higher). Needs its own app context + DB session since it's
    running outside the original request."""

    with app.app_context():

        participant = Participant.query.get(participant_id)

        if participant is None:
            return

        sent = send_qr_email(participant)

        participant.email_sent = sent
        db.session.commit()


def pick_smallest_team() -> str:
    """
    Team with the fewest members.

    Ties are broken using the order in TEAMS, so the six teams are
    assigned in this order when all counts are equal:
        Bolt Billa
        Elbow Ekambaram
        Jacketed Jackson
        Nozzle Nesamani
        Reducer Reeta
        Valve Vallaiyapathi
    """

    counts = {
        team_name: Participant.query.filter_by(team=team_name).count()
        for team_name in TEAMS
    }

    return min(
        TEAMS,
        key=lambda team_name: (
            counts[team_name],
            TEAMS.index(team_name)
        )
    )


def process_scan(code: str):

    """Core scan logic shared by the browser route (/scan/<code>) and the
    JSON API used by the webcam scan station (/api/scan/<code>).
    Returns (participant_or_None, newly_assigned: bool)."""

    participant = Participant.query.filter_by(
        unique_code=code
    ).first()

    if not participant:
        return None, False

    newly_assigned = False

    with assign_lock:

        db.session.refresh(participant)

        if participant.team is None:

            participant.team = pick_smallest_team()
            participant.scanned_at = datetime.utcnow()

            newly_assigned = True

            db.session.commit()

    return participant, newly_assigned


def process_beer_scan(code: str):

    """Marks a participant's beer as collected. This is a completely
    separate flag from team-entry scanning (process_scan above) — the same
    physical QR code is scanned at both stations, but scanning it at the
    beer counter never touches team assignment, and vice versa.

    If the participant said "No" to beer at registration, this is a HARD
    BLOCK: beer_received is never set to True, no matter how many times
    their code is scanned here.

    Returns (participant_or_None, newly_marked: bool, requested_beer: bool).
    requested_beer tells the beer-station screen whether this person is
    even eligible, so it can show "Not registered for beer" instead of
    marking anything."""

    participant = Participant.query.filter_by(
        unique_code=code
    ).first()

    if not participant:
        return None, False, False

    if not participant.needs_beer:
        # Said "No" at registration — never mark beer as given.
        return participant, False, False

    newly_marked = False

    with assign_lock:

        db.session.refresh(participant)

        if not participant.beer_received:

            participant.beer_received = True
            participant.beer_scanned_at = datetime.utcnow()

            newly_marked = True

            db.session.commit()

    return participant, newly_marked, True


def bulk_send_worker(participant_ids):

    """Sends the QR email to a list of participants one at a time, in a
    single background thread, with a short pause between sends. This backs
    the admin "Send QR to all pending" button.

    Deliberately sequential (not one thread per participant) — Gmail SMTP
    will throttle or start failing sends if hit with a big burst all at
    once, which is exactly the risk with 500 registrants. Pacing them out
    keeps the whole batch reliable even if it takes a few minutes longer."""

    with app.app_context():

        for participant_id in participant_ids:

            participant = Participant.query.get(participant_id)

            if participant is None:
                continue

            sent = send_qr_email(participant)

            participant.email_sent = sent
            db.session.commit()

            # Gentle pacing to stay well under Gmail's sending rate limits.
            time.sleep(1.5)


def admin_required(view_func):

    @wraps(view_func)
    def wrapped(*args, **kwargs):

        if not session.get('is_admin'):
            return redirect(
                url_for(
                    'admin_login',
                    next=request.path
                )
            )

        return view_func(*args, **kwargs)

    return wrapped


# ---------------------------- Routes ----------------------------


@app.route('/')
def index():

    return render_template(
        'register.html',
        event_name=EVENT_NAME,
        event_date=EVENT_DATE,
        event_venue=EVENT_VENUE,
        teams=TEAMS
    )


@app.route('/register', methods=['POST'])
def register():

    emp_id = request.form.get(
        'emp_id',
        ''
    ).strip()

    name = request.form.get(
        'name',
        ''
    ).strip()

    email = request.form.get(
        'email',
        ''
    ).strip().lower()

    phone = request.form.get(
        'phone',
        ''
    ).strip()

    needs_bus_raw = request.form.get(
        'needs_bus',
        ''
    ).strip().lower()

    needs_breakfast_raw = request.form.get(
        'needs_breakfast',
        ''
    ).strip().lower()

    needs_beer_raw = request.form.get(
        'needs_beer',
        ''
    ).strip().lower()

    food_preference_raw = request.form.get(
        'food_preference',
        ''
    ).strip().lower()

    if not emp_id or not name or not email:

        flash(
            'Employee ID, name, and email are all required.',
            'error'
        )

        return redirect(url_for('index'))

    if (
        needs_bus_raw not in ('yes', 'no')
        or needs_breakfast_raw not in ('yes', 'no')
        or needs_beer_raw not in ('yes', 'no')
    ):

        flash(
            'Please answer the bus, breakfast, and beer questions.',
            'error'
        )

        return redirect(url_for('index'))

    # Every participant now gets a meal, so veg/non-veg is always required.
    if food_preference_raw not in ('veg', 'nonveg'):

        flash(
            'Please let us know if you prefer veg or non-veg.',
            'error'
        )

        return redirect(url_for('index'))

    if not email.endswith(ALLOWED_EMAIL_DOMAINS):

        flash(
            'Please register with your @ten.com or @external.ten.com '
            'email address.',
            'error'
        )

        return redirect(url_for('index'))

    if not emp_id.isdigit():

        flash(
            'Employee ID must contain numbers only.',
            'error'
        )

        return redirect(url_for('index'))

    if Participant.query.filter_by(
        email=email
    ).first():

        flash(
            'This email is already registered. '
            'Check your inbox for your QR code.',
            'error'
        )

        return redirect(url_for('index'))

    participant = Participant(
        emp_id=emp_id,
        name=name,
        email=email,
        phone=phone,
        unique_code=str(uuid.uuid4()),
        needs_bus=(needs_bus_raw == 'yes'),
        needs_breakfast=(needs_breakfast_raw == 'yes'),
        needs_beer=(needs_beer_raw == 'yes'),
        needs_food=True,
        food_preference=food_preference_raw,
    )

    db.session.add(participant)
    db.session.commit()

    # Send the QR email in the background so this request doesn't block on
    # the Gmail SMTP round-trip (image generation + SMTP auth + send can
    # take several seconds, longer under load). email_sent starts False and
    # is flipped to True/False for real once send_qr_email_async finishes.
    threading.Thread(
        target=send_qr_email_async,
        args=(participant.id,),
        daemon=True
    ).start()

    flash(
        f'Registered! Your QR code is on its way to {email} — '
        'it may take a minute to arrive.',
        'success'
    )

    return redirect(url_for('index'))


@app.route('/scan/<code>')
def scan(code):

    """Organizer-facing endpoint. Opened automatically when a QR code is
    scanned with a phone camera or any QR scanner app (since the QR encodes
    this URL directly). Assigns a team on first scan, just displays it on
    subsequent scans."""

    participant, newly_assigned = process_scan(code)

    if not participant:

        return render_template(
            'scan_result.html',
            found=False
        ), 404

    return render_template(
        'scan_result.html',
        found=True,
        participant=participant,
        newly_assigned=newly_assigned,
    )


@app.route('/api/scan/<code>')
def api_scan(code):

    """JSON version of the scan endpoint, used by the webcam-based
    /scan-station page so it can scan continuously without navigating away."""

    participant, newly_assigned = process_scan(code)

    if not participant:
        return jsonify({
            'found': False
        }), 404

    return jsonify({
        'found': True,
        'name': participant.name,
        'email': participant.email,
        'team': participant.team,
        'newly_assigned': newly_assigned,
    })


@app.route('/scan-station')
def scan_station():

    """A webcam-driven scanning page for a fixed organizer desk (laptop or
    tablet). Continuously scans QR codes via the device camera in-browser,
    without needing a separate scanner app."""

    return render_template(
        'scan_station.html',
        event_name=EVENT_NAME,
        teams=TEAMS
    )


@app.route('/api/beer-scan/<code>')
def api_beer_scan(code):

    """JSON endpoint for the beer counter's webcam scan station
    (/beer-station). Scans the SAME physical QR code as team entry, but
    marks a completely separate beer_received flag instead of touching
    team assignment."""

    participant, newly_marked, requested_beer = process_beer_scan(code)

    if not participant:
        return jsonify({
            'found': False
        }), 404

    return jsonify({
        'found': True,
        'name': participant.name,
        'team': participant.team,
        'newly_marked': newly_marked,
        'requested_beer': requested_beer,
    })


@app.route('/beer-station')
def beer_station():

    """Webcam-driven scan station for the beer counter — a separate desk
    from team-entry scanning. Point the same QR code at this page's camera
    to mark that participant's beer as collected."""

    return render_template(
        'beer_station.html',
        event_name=EVENT_NAME
    )


@app.route('/counts')
def counts():

    result = {
        team_name: Participant.query.filter_by(
            team=team_name
        ).count()
        for team_name in TEAMS
    }

    result['total_registered'] = Participant.query.count()

    result['total_scanned'] = Participant.query.filter(
        Participant.team.isnot(None)
    ).count()

    result['unscanned'] = (
        result['total_registered']
        - result['total_scanned']
    )

    return jsonify(result)


@app.route('/dashboard')
def dashboard():

    return render_template(
        'dashboard.html',
        event_name=EVENT_NAME,
        teams=TEAMS
    )


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():

    if request.method == 'POST':

        password = request.form.get(
            'password',
            ''
        )

        if password == ADMIN_PASSWORD:

            session['is_admin'] = True

            next_url = (
                request.args.get('next')
                or url_for('admin')
            )

            return redirect(next_url)

        flash(
            'Incorrect password.',
            'error'
        )

    return render_template(
        'admin_login.html',
        event_name=EVENT_NAME
    )


@app.route('/admin/logout')
def admin_logout():

    session.pop(
        'is_admin',
        None
    )

    return redirect(
        url_for('admin_login')
    )


@app.route('/admin')
@admin_required
def admin():

    """Full participant list for organizers: names, emails, assigned team,
    registration/scan timestamps, and whether the confirmation email sent."""

    q = request.args.get(
        'q',
        ''
    ).strip()

    # IMPORTANT:
    # Do NOT use .upper() because team names contain mixed-case words.
    team_filter = request.args.get(
        'team',
        ''
    ).strip()

    query = Participant.query

    if q:

        like = f"%{q}%"

        query = query.filter(
            db.or_(
                Participant.name.ilike(like),
                Participant.email.ilike(like)
            )
        )

    if team_filter in TEAMS:

        query = query.filter_by(
            team=team_filter
        )

    elif team_filter == 'UNASSIGNED':

        query = query.filter(
            Participant.team.is_(None)
        )

    elif team_filter == 'SCANNED':

        query = query.filter(
            Participant.scanned_at.isnot(None)
        )

    participants = query.order_by(
        Participant.registered_at.desc()
    ).all()

    team_counts = {
        team_name: Participant.query.filter_by(
            team=team_name
        ).count()
        for team_name in TEAMS
    }

    return render_template(
        'admin.html',
        event_name=EVENT_NAME,
        participants=participants,
        team_counts=team_counts,
        teams=TEAMS,
        q=q,
        team_filter=team_filter,
        total=Participant.query.count(),
    )


@app.route('/admin/upload-db', methods=['POST'])
@admin_required
def admin_upload_db():

    """Lets an organizer restore the database from a previously downloaded
    backup, straight from the browser. The current live database is backed
    up first (never silently overwritten without a safety copy)."""

    import sqlite3
    import tempfile

    uploaded = request.files.get(
        'db_file'
    )

    if not uploaded or uploaded.filename == '':

        flash(
            'No file selected.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    # IMPORTANT: don't reconstruct this path manually — Flask-SQLAlchemy
    # resolves a relative 'sqlite:///event.db' URI against app.instance_path
    # (an "instance/" subfolder), not the project root. Asking the engine
    # directly guarantees we always touch the exact same file the live app
    # is actually reading and writing.
    db_path = db.engine.url.database

    # Save the upload to a temp file first and verify it's a real, valid
    # SQLite database with a participant table before touching anything live.

    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix='.db'
    )

    os.close(tmp_fd)

    uploaded.save(tmp_path)

    try:

        test_conn = sqlite3.connect(
            tmp_path
        )

        count = test_conn.execute(
            "SELECT COUNT(*) FROM participant"
        ).fetchone()[0]

        test_conn.close()

    except Exception as e:

        os.remove(tmp_path)

        flash(
            f'Upload rejected — not a valid database file ({e}).',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    # Safety copy of what's currently live, timestamped, before overwriting.

    timestamp = datetime.utcnow().strftime(
        '%Y%m%d_%H%M%S'
    )

    safety_path = (
        f"{db_path}.before_upload_{timestamp}.bak"
    )

    try:

        if os.path.exists(db_path):

            src = sqlite3.connect(
                db_path
            )

            dst = sqlite3.connect(
                safety_path
            )

            src.backup(dst)

            src.close()
            dst.close()

    except Exception as e:

        os.remove(tmp_path)

        flash(
            f'Could not back up current database, '
            f'aborted for safety ({e}).',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    # All existing SQLAlchemy connections need to release the file first.

    db.session.remove()
    db.engine.dispose()

    try:

        os.replace(
            tmp_path,
            db_path
        )

    except Exception as e:

        flash(
            f'Upload saved but could not replace the live database ({e}). '
            f'Check file permissions on {db_path}.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    flash(
        f'Database restored successfully — '
        f'{count} participant record(s) loaded.',
        'success'
    )

    return redirect(
        url_for('admin')
    )


@app.route(
    '/admin/edit/<int:participant_id>',
    methods=['GET', 'POST']
)
@admin_required
def admin_edit(participant_id):

    """Lets an organizer correct a participant's details
    (typos in name/email/phone/emp_id) or manually change
    their assigned team."""

    participant = Participant.query.get_or_404(
        participant_id
    )

    if request.method == 'POST':

        emp_id = request.form.get(
            'emp_id',
            ''
        ).strip()

        name = request.form.get(
            'name',
            ''
        ).strip()

        email = request.form.get(
            'email',
            ''
        ).strip().lower()

        phone = request.form.get(
            'phone',
            ''
        ).strip()

        # Do NOT uppercase team names.
        team = request.form.get(
            'team',
            ''
        ).strip()

        team = team if team in TEAMS else None

        needs_bus = request.form.get('needs_bus', 'no').strip().lower() == 'yes'
        needs_breakfast = request.form.get('needs_breakfast', 'no').strip().lower() == 'yes'
        needs_beer = request.form.get('needs_beer', 'no').strip().lower() == 'yes'
        beer_received = request.form.get('beer_received', 'no').strip().lower() == 'yes'

        food_preference = request.form.get(
            'food_preference', ''
        ).strip().lower()

        food_preference = (
            food_preference
            if food_preference in ('veg', 'nonveg')
            else None
        )

        if not emp_id or not name or not email:

            flash(
                'All fields are required except phone and team.',
                'error'
            )

            return redirect(
                url_for(
                    'admin_edit',
                    participant_id=participant.id
                )
            )

        if not emp_id.isdigit():

            flash(
                'Employee ID must contain numbers only.',
                'error'
            )

            return redirect(
                url_for(
                    'admin_edit',
                    participant_id=participant.id
                )
            )

        existing = Participant.query.filter(
            Participant.email == email,
            Participant.id != participant.id
        ).first()

        if existing:

            flash(
                'Another participant already uses that email.',
                'error'
            )

            return redirect(
                url_for(
                    'admin_edit',
                    participant_id=participant.id
                )
            )

        team_changed = participant.team != team

        participant.emp_id = emp_id
        participant.name = name
        participant.email = email
        participant.phone = phone
        participant.team = team
        participant.needs_bus = needs_bus
        participant.needs_breakfast = needs_breakfast
        participant.needs_beer = needs_beer
        participant.needs_food = True
        participant.food_preference = food_preference

        # Keep scanned_at consistent with whether a team is actually set —
        # so the "Scanned / checked in" filter stays accurate after a
        # manual edit (e.g. clearing a team, or assigning one by hand).

        if team and not participant.scanned_at:

            participant.scanned_at = datetime.utcnow()

        elif not team:

            participant.scanned_at = None

        # Same idea for the beer flag: keep beer_scanned_at consistent with
        # whichever way the admin flips beer_received by hand.

        if beer_received and not participant.beer_received:

            participant.beer_scanned_at = datetime.utcnow()

        elif not beer_received:

            participant.beer_scanned_at = None

        participant.beer_received = beer_received

        db.session.commit()

        flash(
            f'Updated {participant.name}.'
            + (
                ' Team changed.'
                if team_changed
                else ''
            ),
            'success'
        )

        return redirect(
            url_for('admin')
        )

    return render_template(
        'admin_edit.html',
        event_name=EVENT_NAME,
        participant=participant,
        teams=TEAMS
    )


@app.route('/admin/qr/<int:participant_id>')
@admin_required
def admin_download_qr(participant_id):

    """Downloads one participant's QR code as a PNG, named after them —
    e.g. for reprinting a lost QR or handing it out at a walk-in desk."""

    participant = Participant.query.get_or_404(
        participant_id
    )

    scan_url = (
        f"{BASE_URL}/scan/{participant.unique_code}"
    )

    qr_buf = generate_qr_image(
        scan_url,
        caption_top=EVENT_NAME,
        caption_bottom=participant.name
    )

    name_part = safe_filename_part(
        participant.name
    )

    emp_part = safe_filename_part(
        participant.emp_id
        or str(participant.id)
    )

    filename = (
        f"{emp_part}_{name_part}_QR.png"
    )

    return send_file(
        qr_buf,
        as_attachment=True,
        download_name=filename,
        mimetype='image/png'
    )


@app.route('/admin/qr-all')
@admin_required
def admin_download_all_qr():

    """Downloads every participant's QR code at once as a single ZIP file,
    each PNG named after that participant."""

    import zipfile

    zip_buf = io.BytesIO()

    with zipfile.ZipFile(
        zip_buf,
        'w',
        zipfile.ZIP_DEFLATED
    ) as zf:

        used_names = set()

        for participant in Participant.query.order_by(
            Participant.id
        ).all():

            scan_url = (
                f"{BASE_URL}/scan/{participant.unique_code}"
            )

            qr_buf = generate_qr_image(
                scan_url,
                caption_top=EVENT_NAME,
                caption_bottom=participant.name
            )

            name_part = safe_filename_part(
                participant.name
            )

            emp_part = safe_filename_part(
                participant.emp_id
                or str(participant.id)
            )

            filename = (
                f"{emp_part}_{name_part}_QR.png"
            )

            # guard against two participants producing the same filename

            final_name = filename
            counter = 2

            while final_name in used_names:

                final_name = (
                    f"{emp_part}_{name_part}_QR_{counter}.png"
                )

                counter += 1

            used_names.add(final_name)

            zf.writestr(
                final_name,
                qr_buf.read()
            )

    zip_buf.seek(0)

    timestamp = datetime.utcnow().strftime(
        '%Y%m%d_%H%M%S'
    )

    return send_file(
        zip_buf,
        as_attachment=True,
        download_name=f'all_qr_codes_{timestamp}.zip',
        mimetype='application/zip',
    )


@app.route('/admin/export-excel')
@admin_required
def admin_export_excel():

    """Exports all participant data as a formatted .xlsx file — easier to
    share with organizers who want to filter/sort in Excel rather than
    working from the raw .db file."""

    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = Workbook()

    ws = wb.active
    ws.title = "Participants"

    headers = [
        '#',
        'Emp ID',
        'Name',
        'Email',
        'Phone',
        'Team',
        'Registered At',
        'Scanned At',
        'Email Sent',
        'Needs Bus',
        'Needs Breakfast',
        'Needs Beer',
        'Beer Given',
        'Diet'
    ]

    ws.append(headers)

    header_fill = PatternFill(
        start_color="2563EB",
        end_color="2563EB",
        fill_type="solid"
    )

    header_font = Font(
        color="FFFFFF",
        bold=True
    )

    for col_idx in range(
        1,
        len(headers) + 1
    ):

        cell = ws.cell(
            row=1,
            column=col_idx
        )

        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(
            horizontal="center"
        )

    participants = Participant.query.order_by(
        Participant.id
    ).all()

    for p in participants:

        email = (
            p.email
            if not p.email.endswith('@placeholder.local')
            else ''
        )

        ws.append([
            p.id,
            p.emp_id or '',
            p.name,
            email,
            p.phone or '',
            p.team or 'Unassigned',
            p.registered_at.strftime(
                '%d %b %Y, %H:%M'
            ) if p.registered_at else '',
            p.scanned_at.strftime(
                '%d %b %Y, %H:%M'
            ) if p.scanned_at else '',
            'Sent' if p.email_sent else 'Not sent',
            'Yes' if p.needs_bus else 'No',
            'Yes' if p.needs_breakfast else 'No',
            'Yes' if p.needs_beer else 'No',
            'Yes' if p.beer_received else 'No',
            (
                'Veg' if p.food_preference == 'veg'
                else 'Non-Veg' if p.food_preference == 'nonveg'
                else 'N/A'
            ),
        ])

    # Increased Team column width because team names are longer.

    widths = [
        5,
        14,
        22,
        28,
        15,
        24,
        20,
        20,
        12,
        10,
        10,
        10,
        12,
        10
    ]

    for i, w in enumerate(
        widths,
        start=1
    ):

        ws.column_dimensions[
            get_column_letter(i)
        ].width = w

    buf = io.BytesIO()

    wb.save(buf)

    buf.seek(0)

    timestamp = datetime.utcnow().strftime(
        '%Y%m%d_%H%M%S'
    )

    return send_file(
        buf,
        as_attachment=True,
        download_name=f'participants_{timestamp}.xlsx',
        mimetype=(
            'application/vnd.openxmlformats-officedocument.'
            'spreadsheetml.sheet'
        ),
    )


@app.route('/admin/download-db')
@admin_required
def admin_download_db():

    """Lets an organizer download a safe snapshot of the database straight
    from the browser — no SSH key or terminal needed. Uses SQLite's own
    backup API so it's consistent even while the live app is writing to it."""

    import sqlite3
    import tempfile

    # IMPORTANT: don't reconstruct this path manually — Flask-SQLAlchemy
    # resolves a relative 'sqlite:///event.db' URI against app.instance_path
    # (an "instance/" subfolder), not the project root. Asking the engine
    # directly guarantees we always touch the exact same file the live app
    # is actually reading and writing.

    db_path = db.engine.url.database

    tmp_fd, tmp_path = tempfile.mkstemp(
        suffix='.db'
    )

    os.close(tmp_fd)

    src = sqlite3.connect(
        db_path
    )

    dst = sqlite3.connect(
        tmp_path
    )

    src.backup(dst)

    src.close()
    dst.close()

    timestamp = datetime.utcnow().strftime(
        '%Y%m%d_%H%M%S'
    )

    return send_file(
        tmp_path,
        as_attachment=True,
        download_name=f'event_backup_{timestamp}.db',
        mimetype='application/x-sqlite3',
    )


@app.route('/admin/add', methods=['POST'])
@admin_required
def admin_add():

    """Lets an organizer manually add a participant from the /admin page —
    e.g. for a walk-in registration or someone who couldn't use the form.
    A QR code is generated and, if email is filled in, sent the same way
    as self-service registration."""

    emp_id = request.form.get(
        'emp_id',
        ''
    ).strip()

    name = request.form.get(
        'name',
        ''
    ).strip()

    email = request.form.get(
        'email',
        ''
    ).strip().lower()

    phone = request.form.get(
        'phone',
        ''
    ).strip()

    # Do NOT uppercase team names.
    team = request.form.get(
        'team',
        ''
    ).strip()

    team = team if team in TEAMS else None

    # Optional for a manually-added walk-in — default to "No" if left blank.
    needs_bus = request.form.get('needs_bus', 'no').strip().lower() == 'yes'
    needs_breakfast = request.form.get('needs_breakfast', 'no').strip().lower() == 'yes'
    needs_beer = request.form.get('needs_beer', 'no').strip().lower() == 'yes'

    # Every participant gets a meal now, so this is always required.
    food_preference = request.form.get(
        'food_preference', ''
    ).strip().lower()

    if food_preference not in ('veg', 'nonveg'):

        flash(
            'Please choose Veg or Non-Veg for the participant.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    if not emp_id or not name or not email:

        flash(
            'All fields are required except phone and team.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    if not emp_id.isdigit():

        flash(
            'Employee ID must contain numbers only.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    if Participant.query.filter_by(
        email=email
    ).first():

        flash(
            'A participant with that email already exists.',
            'error'
        )

        return redirect(
            url_for('admin')
        )

    participant = Participant(
        emp_id=emp_id,
        name=name,
        email=email,
        phone=phone,
        unique_code=str(uuid.uuid4()),
        team=team,
        scanned_at=datetime.utcnow()
        if team
        else None,
        needs_bus=needs_bus,
        needs_breakfast=needs_breakfast,
        needs_beer=needs_beer,
        needs_food=True,
        food_preference=food_preference,
    )

    db.session.add(participant)
    db.session.commit()

    threading.Thread(
        target=send_qr_email_async,
        args=(participant.id,),
        daemon=True
    ).start()

    flash(
        f'Participant added. QR code is being sent to {email}.',
        'success'
    )

    return redirect(
        url_for('admin')
    )


@app.route(
    '/admin/resend/<int:participant_id>',
    methods=['POST']
)
@admin_required
def admin_resend(participant_id):

    """Re-sends the QR code email to a participant — for anyone whose first
    email failed, or who just wants it again. Uses their existing unique_code,
    so the QR still points to the same /scan/<code> link and won't create a
    second, different code for the same person."""

    participant = Participant.query.get_or_404(
        participant_id
    )

    if (
        not participant.email
        or participant.email.endswith(
            '@placeholder.local'
        )
    ):

        return redirect(
            url_for('admin')
        )

    threading.Thread(
        target=send_qr_email_async,
        args=(participant.id,),
        daemon=True
    ).start()

    flash(
        f'Resending QR code to {participant.email}.',
        'success'
    )

    return redirect(
        url_for('admin')
    )


@app.route('/admin/bulk-send', methods=['POST'])
@admin_required
def admin_bulk_send():

    """Sends the QR code email to every participant who doesn't have one
    yet (email_sent is False), all in one go. Runs in a single background
    thread that paces the sends out (see bulk_send_worker), so this request
    returns immediately instead of the admin's browser sitting there while
    hundreds of emails go out one by one."""

    participants = Participant.query.filter(
        Participant.email_sent.is_(False)
    ).filter(
        ~Participant.email.endswith('@placeholder.local')
    ).all()

    participant_ids = [p.id for p in participants]

    if not participant_ids:

        flash(
            'Everyone with an email address already has their QR code.',
            'success'
        )

        return redirect(url_for('admin'))

    threading.Thread(
        target=bulk_send_worker,
        args=(participant_ids,),
        daemon=True
    ).start()

    flash(
        f'Sending QR codes to {len(participant_ids)} participant(s) in the '
        'background — refresh this page in a few minutes to see progress.',
        'success'
    )

    return redirect(url_for('admin'))


@app.route('/admin/random-split', methods=['POST'])
@admin_required
def admin_random_split():

    """Randomly assigns a team to every registered participant who doesn't
    already have one — an alternative to waiting for each person to have
    their QR scanned at the door. Ties are broken by shuffling the whole
    list first, then handing out teams round-robin, so the split stays as
    even as possible."""

    unassigned = Participant.query.filter(
        Participant.team.is_(None)
    ).all()

    if not unassigned:

        flash(
            'Everyone already has a team assigned.',
            'error'
        )

        return redirect(url_for('admin'))

    random.shuffle(unassigned)

    now = datetime.utcnow()

    for i, participant in enumerate(unassigned):

        participant.team = TEAMS[i % len(TEAMS)]
        participant.scanned_at = now

    db.session.commit()

    flash(
        f'Randomly split {len(unassigned)} participant(s) across '
        f'{len(TEAMS)} teams.',
        'success'
    )

    return redirect(url_for('admin'))


@app.route(
    '/admin/delete/<int:participant_id>',
    methods=['POST']
)
@admin_required
def admin_delete(participant_id):

    """Permanently removes a participant record from the database."""

    participant = Participant.query.get_or_404(
        participant_id
    )

    db.session.delete(
        participant
    )

    db.session.commit()

    return redirect(
        url_for('admin')
    )


if __name__ == '__main__':

    # debug=True is for local development only — turn off in production.

    app.run(
        debug=True,
        host='0.0.0.0',
        port=5000
    )
