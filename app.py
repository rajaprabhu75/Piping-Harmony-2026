import os
import io
import uuid
import threading
from datetime import datetime

from functools import wraps
from flask import Flask, request, redirect, url_for, render_template, jsonify, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_mail import Mail, Message
import qrcode
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change-this-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', 'sqlite:///event.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# ---------------- Mail configuration (set these in .env) ----------------
app.config['MAIL_SERVER'] = os.environ.get('MAIL_SERVER', 'smtp.gmail.com')
app.config['MAIL_PORT'] = int(os.environ.get('MAIL_PORT', 587))
app.config['MAIL_USE_TLS'] = os.environ.get('MAIL_USE_TLS', 'True') == 'True'
app.config['MAIL_USE_SSL'] = os.environ.get('MAIL_USE_SSL', 'False') == 'True'
app.config['MAIL_USERNAME'] = os.environ.get('MAIL_USERNAME')
app.config['MAIL_PASSWORD'] = os.environ.get('MAIL_PASSWORD')
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get('MAIL_DEFAULT_SENDER', os.environ.get('MAIL_USERNAME'))

# ---------------- Event details (edit or set via .env) ----------------
EVENT_NAME = os.environ.get('EVENT_NAME', 'Piping harmony 2026')
EVENT_DATE = os.environ.get('EVENT_DATE', 'September 05, 2026')
EVENT_VENUE = os.environ.get('EVENT_VENUE', 'Intercontinental,Chennai Mahabalipuram Road, Chennai, Tamil Nadu 600100')
# BASE_URL must be reachable by whatever device/app the organizers use to scan.
# For local testing http://127.0.0.1:5000 is fine. For a real event, deploy the
# app (e.g. on a small VPS or ngrok tunnel) and put that public URL here.
BASE_URL = os.environ.get('BASE_URL', 'http://127.0.0.1:5000')

# Password organizers use to view the participant data (/admin). Change this
# in .env — do not leave the default for a real event.
ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin123')

TEAMS = ['A', 'B', 'C', 'D']

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
    team = db.Column(db.String(1), nullable=True)  # 'A' / 'B' / 'C' / 'D' / None
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)
    scanned_at = db.Column(db.DateTime, nullable=True)
    email_sent = db.Column(db.Boolean, default=False)

    def to_dict(self):
        return {
            'id': self.id,
            'emp_id': self.emp_id,
            'name': self.name,
            'email': self.email,
            'unique_code': self.unique_code,
            'team': self.team,
            'registered_at': self.registered_at.isoformat() if self.registered_at else None,
            'scanned_at': self.scanned_at.isoformat() if self.scanned_at else None,
        }


with app.app_context():
    db.create_all()
    # Lightweight migration: if an existing event.db was created before the
    # emp_id column existed, add it in place instead of requiring a wipe.
    with db.engine.connect() as conn:
        existing_cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(participant)")]
        if 'emp_id' not in existing_cols:
            conn.exec_driver_sql("ALTER TABLE participant ADD COLUMN emp_id VARCHAR(30)")


def generate_qr_image(data: str) -> io.BytesIO:
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf


def send_qr_email(participant: 'Participant') -> bool:
    """Emails the participant a QR code that encodes the /scan/<code> URL,
    so any organizer's phone camera / QR app can open it directly."""
    scan_url = f"{BASE_URL}/scan/{participant.unique_code}"
    qr_buf = generate_qr_image(scan_url)

    subject = f"Your QR Code for {EVENT_NAME}"
    body = f"""Hi {participant.name},

Thank you for registering for {EVENT_NAME}!

Employee ID: {participant.emp_id or '-'}

Event Details:
Date: {EVENT_DATE}
Venue: {EVENT_VENUE}

Your unique QR code is attached to this email. Please bring it (on your
phone or printed) on event day. Any organizer, from any of the four teams,
can scan it and you'll be automatically assigned to a team.

Your registration code: {participant.unique_code}

See you there!
"""
    msg = Message(subject=subject, recipients=[participant.email], body=body)
    msg.attach('event_qr_code.png', 'image/png', qr_buf.read())

    try:
        mail.send(msg)
        return True
    except Exception as e:
        app.logger.error(f"Failed to send email to {participant.email}: {e}")
        return False


def pick_smallest_team() -> str:
    """Team with the fewest members. Ties broken alphabetically (A, B, C, D)."""
    counts = {t: Participant.query.filter_by(team=t).count() for t in TEAMS}
    return min(TEAMS, key=lambda t: (counts[t], t))


def process_scan(code: str):
    """Core scan logic shared by the browser route (/scan/<code>) and the
    JSON API used by the webcam scan station (/api/scan/<code>).
    Returns (participant_or_None, newly_assigned: bool)."""
    participant = Participant.query.filter_by(unique_code=code).first()
    if not participant:
        return None, False

    newly_assigned = False
    with assign_lock:
        db.session.refresh(participant)  # guard against races between requests
        if participant.team is None:
            participant.team = pick_smallest_team()
            participant.scanned_at = datetime.utcnow()
            newly_assigned = True
            db.session.commit()

    return participant, newly_assigned


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if not session.get('is_admin'):
            return redirect(url_for('admin_login', next=request.path))
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
    )


@app.route('/register', methods=['POST'])
def register():
    emp_id = request.form.get('emp_id', '').strip()
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()

    if not emp_id or not name or not email:
        return redirect(url_for('index'))

    if Participant.query.filter_by(email=email).first():
        return redirect(url_for('index'))

    participant = Participant(
        emp_id=emp_id,
        name=name,
        email=email,
        phone=phone,
        unique_code=str(uuid.uuid4()),
    )
    db.session.add(participant)
    db.session.commit()

    sent = send_qr_email(participant)
    participant.email_sent = sent
    db.session.commit()

    return redirect(url_for('index'))


@app.route('/scan/<code>')
def scan(code):
    """Organizer-facing endpoint. Opened automatically when a QR code is
    scanned with a phone camera or any QR scanner app (since the QR encodes
    this URL directly). Assigns a team on first scan, just displays it on
    subsequent scans."""
    participant, newly_assigned = process_scan(code)
    if not participant:
        return render_template('scan_result.html', found=False), 404

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
        return jsonify({'found': False}), 404
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
    return render_template('scan_station.html', event_name=EVENT_NAME)


@app.route('/counts')
def counts():
    result = {t: Participant.query.filter_by(team=t).count() for t in TEAMS}
    result['total_registered'] = Participant.query.count()
    result['total_scanned'] = Participant.query.filter(Participant.team.isnot(None)).count()
    result['unscanned'] = result['total_registered'] - result['total_scanned']
    return jsonify(result)


@app.route('/dashboard')
def dashboard():
    return render_template('dashboard.html', event_name=EVENT_NAME)


@app.route('/admin/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            session['is_admin'] = True
            next_url = request.args.get('next') or url_for('admin')
            return redirect(next_url)
        flash('Incorrect password.', 'error')
    return render_template('admin_login.html', event_name=EVENT_NAME)


@app.route('/admin/logout')
def admin_logout():
    session.pop('is_admin', None)
    return redirect(url_for('admin_login'))


@app.route('/admin')
@admin_required
def admin():
    """Full participant list for organizers: names, emails, assigned team,
    registration/scan timestamps, and whether the confirmation email sent."""
    q = request.args.get('q', '').strip()
    team_filter = request.args.get('team', '').strip().upper()

    query = Participant.query
    if q:
        like = f"%{q}%"
        query = query.filter(db.or_(Participant.name.ilike(like), Participant.email.ilike(like)))
    if team_filter in TEAMS:
        query = query.filter_by(team=team_filter)
    elif team_filter == 'UNASSIGNED':
        query = query.filter(Participant.team.is_(None))

    participants = query.order_by(Participant.registered_at.desc()).all()
    team_counts = {t: Participant.query.filter_by(team=t).count() for t in TEAMS}

    return render_template(
        'admin.html',
        event_name=EVENT_NAME,
        participants=participants,
        team_counts=team_counts,
        q=q,
        team_filter=team_filter,
        total=Participant.query.count(),
    )


@app.route('/admin/add', methods=['POST'])
@admin_required
def admin_add():
    """Lets an organizer manually add a participant from the /admin page —
    e.g. for a walk-in registration or someone who couldn't use the form.
    A QR code is generated and, if email is filled in, sent the same way
    as self-service registration."""
    emp_id = request.form.get('emp_id', '').strip()
    name = request.form.get('name', '').strip()
    email = request.form.get('email', '').strip().lower()
    phone = request.form.get('phone', '').strip()
    team = request.form.get('team', '').strip().upper()
    team = team if team in TEAMS else None

    if not emp_id or not name:
        return redirect(url_for('admin'))

    if email and Participant.query.filter_by(email=email).first():
        return redirect(url_for('admin'))

    participant = Participant(
        emp_id=emp_id,
        name=name,
        # a manually-added participant may not have an email; unique_code
        # still needs a placeholder-free unique value regardless
        email=email or f"no-email-{uuid.uuid4().hex[:8]}@placeholder.local",
        phone=phone,
        unique_code=str(uuid.uuid4()),
        team=team,
        scanned_at=datetime.utcnow() if team else None,
    )
    db.session.add(participant)
    db.session.commit()

    if email:
        sent = send_qr_email(participant)
        participant.email_sent = sent
        db.session.commit()

    return redirect(url_for('admin'))


@app.route('/admin/resend/<int:participant_id>', methods=['POST'])
@admin_required
def admin_resend(participant_id):
    """Re-sends the QR code email to a participant — for anyone whose first
    email failed, or who just wants it again. Uses their existing unique_code,
    so the QR still points to the same /scan/<code> link and won't create a
    second, different code for the same person."""
    participant = Participant.query.get_or_404(participant_id)

    if not participant.email or participant.email.endswith('@placeholder.local'):
        return redirect(url_for('admin'))

    sent = send_qr_email(participant)
    participant.email_sent = sent
    db.session.commit()

    return redirect(url_for('admin'))


@app.route('/admin/delete/<int:participant_id>', methods=['POST'])
@admin_required
def admin_delete(participant_id):
    """Permanently removes a participant record from the database."""
    participant = Participant.query.get_or_404(participant_id)
    db.session.delete(participant)
    db.session.commit()
    return redirect(url_for('admin'))


if __name__ == '__main__':
    # debug=True is for local development only — turn off in production.
    app.run(debug=True, host='0.0.0.0', port=5000)