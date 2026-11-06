import os
import re
import secrets
import sqlite3
import hashlib
import smtplib
import threading
import time
import logging
import json
import urllib.error
import urllib.request

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
# En Render hay un proxy delante: sin esto todos los visitantes parecen la misma persona
# y el límite de 10 registros por hora se compartiría entre todos.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
app.logger.setLevel(logging.INFO)  # para que los INFO (CHAT OK ...) salgan en los logs de Render
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# Webs que pueden llamar al servidor. Se pueden cambiar en Render con la variable CORS_ORIGINS
# (direcciones separadas por comas, sin barra final).
ALLOWED_ORIGINS = [origin.strip() for origin in os.getenv(
    "CORS_ORIGINS",
    "http://localhost:8000,http://127.0.0.1:8000,http://localhost:5500,http://127.0.0.1:5500,"
    "https://shouyi77.github.io"
).split(",") if origin.strip()]
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS},
                     r"/chat": {"origins": ALLOWED_ORIGINS},
                     r"/enviar-email-promocional": {"origins": ALLOWED_ORIGINS}},
     allow_headers=["Content-Type", "Authorization"])

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if genai and api_key else None
HOTEL_NAME = "La Palma Princess"
HOTEL_EMAIL = os.getenv("HOTEL_EMAIL", "hotelPrincessLaPalma@gmail.com")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
BREVO_API_KEY = os.getenv("BREVO_API_KEY", "").strip()
BREVO_API_URL = os.getenv("BREVO_API_URL", "https://api.brevo.com/v3/smtp/email")
MAIL_FROM = os.getenv("MAIL_FROM") or os.getenv("SMTP_FROM") or HOTEL_EMAIL
DATABASE_PATH = Path(os.getenv("DATABASE_PATH", "reservas.sqlite3"))
HOTEL_TIMEZONE = ZoneInfo("Atlantic/Canary")

ROOMS = {
    "Estándar": {"price": 150, "inventory": 20, "adults": 3, "children": 2, "guests": 4},
    "Familiar Superior": {"price": 185, "inventory": 12, "adults": 4, "children": 2, "guests": 6},
    "Ocean Suite": {"price": 250, "inventory": 8, "adults": 3, "children": 2, "guests": 5},
}
SERVICES = {"spa", "restaurant", "activity"}
SERVICE_CAPACITY = {"spa": 8, "restaurant": 24, "activity": 16}
SERVICE_TIMES = {"spa": ("09:00", "11:00", "13:00", "15:00", "17:00"),
                 "restaurant": ("13:00", "14:00", "19:00", "20:00", "21:00"),
                 "activity": ("09:00", "11:00", "15:00", "17:00")}

# Modelos de la IA del chatbot. Se pueden cambiar SIN tocar el codigo: en Render -> Environment
# -> MODELOS_IA (nombres separados por comas, el primero es el preferido).
MODELOS_IA = [
    m.strip() for m in os.getenv(
        "MODELOS_IA",
        "gemini-3.6-flash,gemini-3.5-flash,gemini-3.1-flash-lite"
    ).split(",") if m.strip()
]


@contextmanager
def connect_db():
    if str(DATABASE_PATH.parent) not in ("", "."):
        DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with connect_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY,
                reference TEXT NOT NULL UNIQUE,
                token_hash TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                checkin TEXT NOT NULL,
                checkout TEXT NOT NULL,
                room TEXT NOT NULL,
                rooms INTEGER NOT NULL,
                adults INTEGER NOT NULL,
                children INTEGER NOT NULL,
                promo TEXT NOT NULL DEFAULT '',
                resident INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT '',
                total REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE INDEX IF NOT EXISTS bookings_dates ON bookings(room, status, checkin, checkout);
            CREATE TABLE IF NOT EXISTS subscribers (
                id INTEGER PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                consent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS service_bookings (
                id INTEGER PRIMARY KEY,
                reference TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                email TEXT NOT NULL,
                service TEXT NOT NULL,
                service_date TEXT NOT NULL,
                service_time TEXT NOT NULL,
                guests INTEGER NOT NULL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(service, service_date, service_time, email)
            );
            CREATE INDEX IF NOT EXISTS service_slots ON service_bookings(service, service_date, service_time);
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
        """)
        # Bases de datos creadas antes de las cuentas: añade la columna user_id si falta.
        for table in ("bookings", "service_bookings"):
            columns = [row["name"] for row in db.execute(f"PRAGMA table_info({table})")]
            if "user_id" not in columns:
                db.execute(f"ALTER TABLE {table} ADD COLUMN user_id INTEGER")
            db.execute(f"CREATE INDEX IF NOT EXISTS {table}_user ON {table}(user_id)")


init_db()


def api_error(message, status):
    return jsonify({"error": message}), status


def json_payload():
    payload = request.get_json(silent=True)
    return payload if isinstance(payload, dict) else None


def valid_date(value):
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
        return parsed if parsed.isoformat() == value else None
    except (TypeError, ValueError):
        return None


def hotel_today():
    return datetime.now(HOTEL_TIMEZONE).date()


def clean_text(value, maximum):
    return str(value or "").strip()[:maximum]


def parse_integer(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value)
    return None


def email_valid(value):
    return len(value) <= 254 and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) is not None


def _enviar_brevo(recipient, subject, body, reply_to=None):
    """Envía con la API de Brevo (HTTPS, puerto 443). Funciona en Render gratis."""
    es_html = body.lstrip().startswith("<")
    payload = {
        "sender": {"name": HOTEL_NAME, "email": MAIL_FROM},
        "to": [{"email": recipient}],
        "subject": subject,
    }
    payload["htmlContent" if es_html else "textContent"] = body
    if reply_to:
        payload["replyTo"] = {"email": reply_to}
    peticion = urllib.request.Request(
        BREVO_API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(peticion, timeout=15) as respuesta:
            return 200 <= respuesta.status < 300
    except urllib.error.HTTPError as error:
        # Aquí sale el motivo real: 401 = clave mala, 400 = remitente sin verificar
        app.logger.error("Brevo rechazó el correo a %s: HTTP %s %s", recipient, error.code,
                         error.read().decode("utf-8", "replace")[:300])
    except (urllib.error.URLError, OSError, ValueError):
        app.logger.exception("No se pudo conectar con Brevo para enviar el correo a %s", recipient)
    return False
 
 
def _enviar_smtp(recipient, subject, body, reply_to=None):
    """SMTP clásico. En Render gratis los puertos 25/465/587 están bloqueados: allí usa Brevo."""
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    port = int(os.getenv("SMTP_PORT", "465"))
    username = os.getenv("SMTP_USERNAME", "hotelprincesslapalmatrabajo@gmail.com")
    password = os.getenv("SMTP_PASSWORD")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SMTP_FROM") or username
    message["To"] = recipient
    if reply_to:
        message["Reply-To"] = reply_to
    if body.lstrip().startswith("<"):
        message.set_content("Este mensaje está en formato HTML.")
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    try:
        usar_ssl = port == 465 or os.getenv("SMTP_SSL") == "1"
        conexion = smtplib.SMTP_SSL(host, port, timeout=15) if usar_ssl else smtplib.SMTP(host, port, timeout=15)
        with conexion as smtp:
            if not usar_ssl and os.getenv("SMTP_STARTTLS", "1") == "1":
                smtp.starttls()
            smtp.login(username, password)
            smtp.send_message(message)
        return True
    except (OSError, smtplib.SMTPException, ValueError):
        app.logger.exception("No se pudo enviar el correo a %s por SMTP", recipient)
        return False
 
 
def send_email(recipient, subject, body, reply_to=None):
    """Envía un correo (HTML si el cuerpo empieza por '<'). Usa Brevo si hay BREVO_API_KEY."""
    if BREVO_API_KEY:
        return _enviar_brevo(recipient, subject, body, reply_to)
    if os.getenv("SMTP_PASSWORD"):
        return _enviar_smtp(recipient, subject, body, reply_to)
    app.logger.warning("Correo NO enviado a %s: falta BREVO_API_KEY (o SMTP_PASSWORD).", recipient)
    return False
 
 
def send_async(recipient, subject, body, reply_to=None):
    """Envía en segundo plano para no hacer esperar al visitante."""
    threading.Thread(target=send_email, args=(recipient, subject, body, reply_to), daemon=True).start()
 
 
def notify_hotel(subject, body, reply_to=None):
    """Aviso al buzón del propietario (HOTEL_EMAIL). 'Responder' contesta al cliente."""
    if HOTEL_EMAIL:
        send_async(HOTEL_EMAIL, "[Web] " + subject, body, reply_to)


def booking_total(room, rooms, adults, children, nights, resident, promo):
    subtotal = ROOMS[room]["price"] * rooms * nights
    subtotal += max(0, adults - 2 * rooms) * 30 * nights + children * 12 * nights
    total = subtotal
    if resident:
        total -= subtotal * 0.10
    if promo:
        total -= subtotal * 0.05
    return round(total, 2)


def valid_occupancy(room, rooms, adults, children):
    capacity = ROOMS[room]
    return (
        adults <= capacity["adults"] * rooms and
        children <= capacity["children"] * rooms and
        adults + children <= capacity["guests"] * rooms
    )


def room_availability(db, room, checkin, checkout):
    reserved = db.execute(
        """SELECT COALESCE(SUM(rooms), 0) FROM bookings
           WHERE room = ? AND status = 'confirmed' AND checkin < ? AND checkout > ?""",
        (room, checkout, checkin),
    ).fetchone()[0]
    return max(0, ROOMS[room]["inventory"] - reserved)


def send_booking_email(email, name, reference, token, checkin, checkout, total):
    management_link = PUBLIC_BASE_URL + "/reservas.html?manage=" + token
    body_html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">Confirmación de Reserva</h1>
            <p>Hola <strong>{escape(name)}</strong>,</p>
            <p>Hemos recibido tu solicitud de reserva en el <strong>Hotel Princess La Palma</strong>. A continuación tienes los detalles:</p>

            <table style="width: 100%; border-collapse: collapse; margin: 20px 0; background: #f8fafb; border: 1px solid #e2e8f0;">
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Referencia:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{reference}</td></tr>
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Entrada:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{checkin}</td></tr>
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Salida:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{checkout}</td></tr>
                <tr><td style="padding: 10px;"><strong>Total orientativo:</strong></td><td style="padding: 10px;"><strong>{total:.2f} EUR</strong></td></tr>
            </table>

            <p><a href="{management_link}" style="background-color: #12303a; color: white; padding: 10px 18px; text-decoration: none; border-radius: 4px; display: inline-block;">Gestionar o Cancelar Reserva</a></p>

            <hr style="border: none; border-top: 1px solid #eeeeee; margin: 20px 0;">
            <p style="font-size: 12px; color: #777;">Si deseas dejar de recibir avisos, haz clic en <a href="{PUBLIC_BASE_URL}/desuscrito.html?email={escape(email)}">darse de baja</a>.</p>
        </div>
    </div>
    """
    return send_email(email, f"Reserva {reference} - {HOTEL_NAME}", body_html)


# ───────────────────────── Cuentas de usuario y sesiones ─────────────────────────
# El navegador guarda un token aleatorio (en localStorage) y lo envía en la cabecera
# "Authorization: Bearer <token>". En la base de datos solo se guarda su hash SHA-256.
SESSION_DAYS = 30
MAX_LOGIN_FAILS = 8
LOGIN_WINDOW = 10 * 60
MAX_REGISTRATIONS = 10
REGISTER_WINDOW = 60 * 60
DUMMY_HASH = generate_password_hash("contraseña-ficticia")
_attempts = {}
_attempts_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc)


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def attempts_count(key, window):
    cutoff = time.time() - window
    with _attempts_lock:
        recent = [t for t in _attempts.get(key, []) if t > cutoff]
        if recent:
            _attempts[key] = recent
        else:
            _attempts.pop(key, None)
        return len(recent)


def attempts_add(key):
    with _attempts_lock:
        _attempts.setdefault(key, []).append(time.time())


def attempts_clear(key):
    with _attempts_lock:
        _attempts.pop(key, None)


def create_session(db, user_id):
    token = secrets.token_urlsafe(32)
    now = utc_now()
    db.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))
    db.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (hash_token(token), user_id, now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()),
    )
    return token


def bearer_token():
    header = request.headers.get("Authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


def current_user(db):
    token = bearer_token()
    if not token:
        return None
    return db.execute(
        """SELECT u.id, u.name, u.email FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = ? AND s.expires_at > ?""",
        (hash_token(token), utc_now().isoformat()),
    ).fetchone()


def user_json(user):
    return {"name": user["name"], "email": user["email"]}


@app.post("/api/auth/register")
def register():
    """Crea la cuenta y deja la sesión iniciada en el mismo paso."""
    data = json_payload()
    if not data:
        return api_error("Envía los datos de registro en formato JSON.", 400)
    name = clean_text(data.get("name"), 100)
    email = clean_text(data.get("email"), 254).lower()
    password = data.get("password")
    if not name or not email_valid(email):
        return api_error("Escribe tu nombre y un email válido.", 400)
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        return api_error("La contraseña debe tener entre 8 y 128 caracteres.", 400)
    if data.get("terms") is not True:
        return api_error("Debes aceptar las condiciones de uso.", 400)
    ip_key = "register|" + (request.remote_addr or "")
    if attempts_count(ip_key, REGISTER_WINDOW) >= MAX_REGISTRATIONS:
        return api_error("Demasiados registros desde esta conexión. Inténtalo más tarde.", 429)
    try:
        with connect_db() as db:
            cursor = db.execute(
                "INSERT INTO users (name, email, password_hash) VALUES (?, ?, ?)",
                (name, email, generate_password_hash(password)),
            )
            token = create_session(db, cursor.lastrowid)
    except sqlite3.IntegrityError:
        return api_error("Ya existe una cuenta con ese email. Inicia sesión.", 409)
    attempts_add(ip_key)

    # Email de bienvenida (si falla el envío, la cuenta se crea igualmente)
    html_bienvenida = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">¡Bienvenido/a a La Palma Princess!</h1>
            <p>Hola <strong>{escape(name)}</strong>,</p>
            <p>Gracias por registrarte. Tu cuenta ha sido creada correctamente.</p>
            <p>Ahora podrás realizar tus reservas de forma más rápida y acceder a ofertas exclusivas.</p>
            <hr style="border: none; border-top: 1px solid #eeeeee; margin: 20px 0;">
            <p style="font-size: 12px; color: #777;">Si deseas darte de baja, haz clic en <a href="{PUBLIC_BASE_URL}/desuscrito.html?email={escape(email)}">darse de baja</a>.</p>
        </div>
    </div>
    """
    send_email(email, "¡Bienvenido/a a La Palma Princess!", html_bienvenida)
    notify_hotel("Nueva alta de usuario", f"Nombre: {name}\nEmail: {email}", reply_to=email)

    return jsonify({"token": token, "user": {"name": name, "email": email}}), 201


@app.post("/api/auth/login")
def login():
    data = json_payload()
    if not data:
        return api_error("Envía los datos de acceso en formato JSON.", 400)
    email = clean_text(data.get("email"), 254).lower()
    password = data.get("password")
    if not email or not isinstance(password, str) or not password:
        return api_error("Escribe tu email y tu contraseña.", 400)
    key = "login|" + (request.remote_addr or "") + "|" + email
    if attempts_count(key, LOGIN_WINDOW) >= MAX_LOGIN_FAILS:
        return api_error("Demasiados intentos fallidos. Espera unos minutos e inténtalo de nuevo.", 429)
    with connect_db() as db:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        # Se comprueba siempre un hash para que el tiempo de respuesta no delate si el email existe.
        valid = check_password_hash(user["password_hash"] if user else DUMMY_HASH, password)
        if not user or not valid:
            attempts_add(key)
            return api_error("Email o contraseña incorrectos.", 401)
        token = create_session(db, user["id"])
    attempts_clear(key)
    return jsonify({"token": token, "user": user_json(user)})


@app.post("/api/auth/logout")
def logout():
    token = bearer_token()
    if token:
        with connect_db() as db:
            db.execute("DELETE FROM sessions WHERE token_hash = ?", (hash_token(token),))
    return jsonify({"message": "Sesión cerrada."})

@app.delete("/api/auth/delete-account")
def delete_account():
    token = bearer_token()
    if not token:
        return api_error("No hay una sesión activa.", 401)
    with connect_db() as db:
        user = current_user(db)
        if not user:
            return api_error("La sesión ha caducado.", 401)
        
        db.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        db.execute("UPDATE bookings SET user_id = NULL WHERE user_id = ?", (user["id"],))
        db.execute("UPDATE service_bookings SET user_id = NULL WHERE user_id = ?", (user["id"],))
        db.execute("DELETE FROM users WHERE id = ?", (user["id"],))
        
        send_async(user["email"], "Cuenta eliminada - La Palma Princess", 
                   f"Hola {user['name']}:\n\nTu cuenta ha sido eliminada permanentemente.")
        notify_hotel("Baja de usuario", f"El usuario {user['name']} ({user['email']}) ha eliminado su cuenta.")

    return jsonify({"message": "Cuenta eliminada permanentemente."}), 200

@app.get("/api/auth/me")
def me():
    with connect_db() as db:
        user = current_user(db)
    if not user:
        return api_error("La sesión ha caducado. Inicia sesión de nuevo.", 401)
    return jsonify({"user": user_json(user)})


@app.get("/api/me/bookings")
def my_bookings():
    with connect_db() as db:
        user = current_user(db)
        if not user:
            return api_error("Inicia sesión para ver tus reservas.", 401)
        rooms = db.execute(
            """SELECT reference, checkin, checkout, room, rooms, adults, children, total, status
               FROM bookings WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT 50""",
            (user["id"],),
        ).fetchall()
        services = db.execute(
            """SELECT reference, service, service_date, service_time, guests
               FROM service_bookings WHERE user_id = ? ORDER BY service_date DESC, service_time DESC LIMIT 50""",
            (user["id"],),
        ).fetchall()
    return jsonify({"bookings": [dict(row) for row in rooms], "services": [dict(row) for row in services]})


@app.post("/api/me/bookings/<reference>/cancel")
def cancel_my_booking(reference):
    with connect_db() as db:
        user = current_user(db)
        if not user:
            return api_error("Inicia sesión para gestionar tus reservas.", 401)
        cursor = db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE reference = ? AND user_id = ? AND status = 'confirmed'",
            (reference.upper(), user["id"]),
        )
        if cursor.rowcount == 0:
            return api_error("No se encontró una reserva activa tuya con esa referencia.", 404)
    email_sent = send_email(
        user["email"], f"Cancelación {reference.upper()} - {HOTEL_NAME}",
        f"Hola {user['name']}:\n\nTu reserva {reference.upper()} ha sido cancelada."
    )
    notify_hotel(f"Reserva cancelada {reference.upper()}",
                 f"Referencia: {reference.upper()}\nCliente: {user['name']}\nEmail: {user['email']}",
                 reply_to=user["email"])
    return jsonify({"message": "Reserva cancelada.", "email_sent": email_sent})


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


@app.get("/api/availability")
def availability():
    checkin = valid_date(request.args.get("checkin"))
    checkout = valid_date(request.args.get("checkout"))
    room = request.args.get("room", "Estándar")
    rooms = parse_integer(request.args.get("rooms", "1"))
    adults = parse_integer(request.args.get("adults", "2"))
    children = parse_integer(request.args.get("children", "0"))
    if None in (rooms, adults, children):
        return api_error("Los huéspedes y las habitaciones deben ser números válidos.", 400)
    resident = request.args.get("resident") == "1"
    promo = request.args.get("promo", "")
    if not checkin or not checkout or checkin < hotel_today() or checkout <= checkin:
        return api_error("Indica fechas válidas y una salida posterior a la entrada.", 400)
    if room not in ROOMS or not 1 <= rooms <= 4 or adults < 1 or adults > 16 or not 0 <= children <= 12:
        return api_error("Revisa el tipo de habitación y el número de huéspedes.", 400)
    if not valid_occupancy(room, rooms, adults, children):
        return api_error("El número de huéspedes supera la capacidad del tipo de habitación.", 400)
    if promo and not re.fullmatch(r"\d{6}", promo):
        return api_error("El código promocional debe tener seis dígitos.", 400)
    nights = (checkout - checkin).days
    with connect_db() as db:
        available = room_availability(db, room, checkin.isoformat(), checkout.isoformat())
    return jsonify({
        "available": available >= rooms,
        "rooms_available": available,
        "nights": nights,
        "total": booking_total(room, rooms, adults, children, nights, resident, promo),
        "currency": "EUR",
        "demo": True,
    })


@app.post("/api/bookings")
def create_booking():
    data = json_payload()
    if not data:
        return api_error("Envía los datos de la reserva en formato JSON.", 400)
    name = clean_text(data.get("name"), 100)
    email = clean_text(data.get("email"), 254)
    checkin = valid_date(data.get("checkin"))
    checkout = valid_date(data.get("checkout"))
    room = clean_text(data.get("room"), 40)
    message = clean_text(data.get("message"), 1000)
    promo = clean_text(data.get("promo"), 6)
    rooms = parse_integer(data.get("rooms", 1))
    adults = parse_integer(data.get("adults", 2))
    children = parse_integer(data.get("children", 0))
    if None in (rooms, adults, children):
        return api_error("El número de huéspedes y habitaciones no es válido.", 400)
    resident_value = data.get("resident", False)
    if not isinstance(resident_value, bool):
        return api_error("El indicador de residente no es válido.", 400)
    resident = resident_value
    if not name or not email_valid(email) or not checkin or not checkout or checkin < hotel_today() or checkout <= checkin:
        return api_error("Revisa el nombre, el email y las fechas de la reserva.", 400)
    if room not in ROOMS or not 1 <= rooms <= 4 or not 1 <= adults <= 16 or not 0 <= children <= 12:
        return api_error("Revisa el tipo de habitación y el número de huéspedes.", 400)
    if not valid_occupancy(room, rooms, adults, children):
        return api_error("El número de huéspedes supera la capacidad del tipo de habitación.", 400)
    if promo and not re.fullmatch(r"\d{6}", promo):
        return api_error("El código promocional debe tener seis dígitos.", 400)

    reference = secrets.token_hex(4).upper()
    token = secrets.token_urlsafe(32)
    total = booking_total(room, rooms, adults, children, (checkout - checkin).days, resident, promo)
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            available = room_availability(db, room, checkin.isoformat(), checkout.isoformat())
            if available < rooms:
                return jsonify({"error": "No hay disponibilidad para esas fechas.", "rooms_available": available}), 409
            user = current_user(db)
            db.execute(
                """INSERT INTO bookings
                   (reference, token_hash, name, email, checkin, checkout, room, rooms, adults,
                    children, promo, resident, message, total, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (reference, hashlib.sha256(token.encode()).hexdigest(), name, email,
                 checkin.isoformat(), checkout.isoformat(), room, rooms, adults,
                 children, promo, int(resident), message, total, user["id"] if user else None),
            )
    except sqlite3.IntegrityError:
        return api_error("No se pudo registrar la solicitud. Inténtalo de nuevo.", 409)

    email_sent = send_booking_email(email, name, reference, token, checkin.isoformat(), checkout.isoformat(), total)
    notify_hotel(
        f"Nueva reserva {reference}",
        f"Referencia: {reference}\nCliente: {name}\nEmail: {email}\n"
        f"Habitación: {room} x{rooms}\nEntrada: {checkin.isoformat()}\nSalida: {checkout.isoformat()}\n"
        f"Adultos: {adults}  Niños: {children}\nResidente: {'sí' if resident else 'no'}\n"
        f"Código promocional: {promo or '-'}\nTotal orientativo: {total:.2f} EUR\n"
        f"Comentarios: {message or '-'}",
        reply_to=email,
    )
    return jsonify({
        "reference": reference,
        "management_token": token,
        "total": total,
        "email_sent": email_sent,
        "message": "Solicitud registrada y confirmación enviada." if email_sent else
                   "Solicitud registrada. La confirmación por correo está pendiente de configurar.",
    }), 201


def find_booking(token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as db:
        return db.execute("SELECT * FROM bookings WHERE token_hash = ?", (token_hash,)).fetchone()


@app.get("/api/bookings/<token>")
def get_booking(token):
    booking = find_booking(token)
    if not booking:
        return api_error("No se encontró una reserva con ese enlace.", 404)
    return jsonify({key: booking[key] for key in (
        "reference", "name", "email", "checkin", "checkout", "room", "rooms",
        "adults", "children", "total", "status"
    )})


@app.post("/api/bookings/<token>/cancel")
def cancel_booking(token):
    with connect_db() as db:
        cursor = db.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE token_hash = ? AND status = 'confirmed'",
            (hashlib.sha256(token.encode()).hexdigest(),),
        )
        if cursor.rowcount == 0:
            return api_error("No se encontró una reserva activa para cancelar.", 404)
        booking = db.execute("SELECT * FROM bookings WHERE token_hash = ?",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    email_sent = send_email(
        booking["email"], f"Cancelación {booking['reference']} - {HOTEL_NAME}",
        f"Hola {booking['name']}:\n\nTu reserva {booking['reference']} ha sido cancelada."
    )
    notify_hotel(f"Reserva cancelada {booking['reference']}",
                 f"Referencia: {booking['reference']}\nCliente: {booking['name']}\nEmail: {booking['email']}",
                 reply_to=booking["email"])
    return jsonify({"message": "Reserva cancelada.", "email_sent": email_sent})


@app.patch("/api/bookings/<token>")
def update_booking(token):
    data = json_payload()
    if not data:
        return api_error("Envía los nuevos datos en formato JSON.", 400)
    booking = find_booking(token)
    if not booking or booking["status"] != "confirmed":
        return api_error("No se encontró una reserva activa para modificar.", 404)
    checkin = valid_date(data.get("checkin", booking["checkin"]))
    checkout = valid_date(data.get("checkout", booking["checkout"]))
    room = clean_text(data.get("room", booking["room"]), 40)
    rooms = parse_integer(data.get("rooms", booking["rooms"]))
    if rooms is None:
        return api_error("El número de habitaciones no es válido.", 400)
    if not checkin or not checkout or checkin < hotel_today() or checkout <= checkin or room not in ROOMS or not 1 <= rooms <= 4:
        return api_error("Revisa las fechas, el tipo de habitación y el número de habitaciones.", 400)
    adults = booking["adults"]
    children = booking["children"]
    if not valid_occupancy(room, rooms, adults, children):
        return api_error("El tipo de habitación elegido no admite a todos los huéspedes.", 400)
    resident = booking["resident"]
    promo = booking["promo"]
    total = booking_total(room, rooms, adults, children, (checkout - checkin).days, resident, promo)
    with connect_db() as db:
        db.execute("BEGIN IMMEDIATE")
        available = room_availability(db, room, checkin.isoformat(), checkout.isoformat())
        if room == booking["room"] and checkin < date.fromisoformat(booking["checkout"]) and checkout > date.fromisoformat(booking["checkin"]):
            available += booking["rooms"]
        if available < rooms:
            return jsonify({"error": "No hay disponibilidad para esos cambios.", "rooms_available": available}), 409
        db.execute(
            "UPDATE bookings SET checkin = ?, checkout = ?, room = ?, rooms = ?, total = ? WHERE id = ?",
            (checkin.isoformat(), checkout.isoformat(), room, rooms, total, booking["id"]),
        )
    email_sent = send_email(
        booking["email"], f"Actualización de reserva {booking['reference']} - {HOTEL_NAME}",
        f"Hola {booking['name']}:\n\nTu reserva se ha actualizado.\n"
        f"Entrada: {checkin.isoformat()}\nSalida: {checkout.isoformat()}\n"
        f"Habitación: {room}\nTotal orientativo: {total:.2f} EUR."
    )
    notify_hotel(
        f"Reserva modificada {booking['reference']}",
        f"Referencia: {booking['reference']}\nCliente: {booking['name']}\nEmail: {booking['email']}\n"
        f"Entrada: {checkin.isoformat()}\nSalida: {checkout.isoformat()}\n"
        f"Habitación: {room} x{rooms}\nTotal orientativo: {total:.2f} EUR",
        reply_to=booking["email"],
    )
    return jsonify({
        "message": "Reserva actualizada.",
        "total": total,
        "email_sent": email_sent,
    })


@app.get("/api/service-slots")
def service_slots():
    service = request.args.get("service", "")
    service_date = valid_date(request.args.get("date"))
    if service not in SERVICES or not service_date or service_date < hotel_today():
        return api_error("Indica un servicio y una fecha futura válidos.", 400)
    with connect_db() as db:
        slots = []
        for service_time in SERVICE_TIMES[service]:
            if service_date == hotel_today() and service_time <= datetime.now(HOTEL_TIMEZONE).strftime("%H:%M"):
                continue
            occupied = db.execute(
                "SELECT COALESCE(SUM(guests), 0) FROM service_bookings WHERE service = ? AND service_date = ? AND service_time = ?",
                (service, service_date.isoformat(), service_time),
            ).fetchone()[0]
            remaining = max(0, SERVICE_CAPACITY[service] - occupied)
            if remaining:
                slots.append({"time": service_time, "available": remaining})
    return jsonify({"service": service, "date": service_date.isoformat(), "slots": slots})


@app.post("/api/service-bookings")
def create_service_booking():
    data = json_payload()
    if not data:
        return api_error("Envía los datos de la cita en formato JSON.", 400)
    name = clean_text(data.get("name"), 100)
    email = clean_text(data.get("email"), 254)
    service = clean_text(data.get("service"), 20)
    service_date = valid_date(data.get("date"))
    service_time = clean_text(data.get("time"), 5)
    notes = clean_text(data.get("notes"), 500)
    guests = parse_integer(data.get("guests", 1))
    if guests is None:
        return api_error("El número de asistentes no es válido.", 400)
    if not name or not email_valid(email) or service not in SERVICES or not service_date or service_date < hotel_today():
        return api_error("Revisa el nombre, el email, el servicio y la fecha.", 400)
    if service_time not in SERVICE_TIMES[service] or not 1 <= guests <= 12:
        return api_error("Elige una hora disponible y un número válido de asistentes.", 400)
    if service_date == hotel_today() and service_time <= datetime.now(HOTEL_TIMEZONE).strftime("%H:%M"):
        return api_error("La hora elegida ya ha pasado.", 400)
    reference = secrets.token_hex(4).upper()
    try:
        with connect_db() as db:
            db.execute("BEGIN IMMEDIATE")
            occupied = db.execute(
                "SELECT COALESCE(SUM(guests), 0) FROM service_bookings WHERE service = ? AND service_date = ? AND service_time = ?",
                (service, service_date.isoformat(), service_time),
            ).fetchone()[0]
            if occupied + guests > SERVICE_CAPACITY[service]:
                return api_error("Ya no quedan plazas suficientes en esa hora.", 409)
            user = current_user(db)
            db.execute(
                """INSERT INTO service_bookings
                   (reference, name, email, service, service_date, service_time, guests, notes, user_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (reference, name, email, service, service_date.isoformat(), service_time, guests, notes,
                 user["id"] if user else None),
            )
    except sqlite3.IntegrityError:
        return api_error("Ya existe una reserva de ese servicio para ese email y horario.", 409)
    email_sent = send_email(
        email, f"Reserva de servicio {reference} - {HOTEL_NAME}",
        f"Hola {name}:\n\nHemos registrado tu reserva de {service} para {service_date.isoformat()} "
        f"a las {service_time} ({guests} asistentes).\nReferencia: {reference}"
    )
    notify_hotel(
        f"Nueva reserva de servicio {reference}",
        f"Referencia: {reference}\nCliente: {name}\nEmail: {email}\nServicio: {service}\n"
        f"Fecha: {service_date.isoformat()} {service_time}\nAsistentes: {guests}\nNotas: {notes or '-'}",
        reply_to=email,
    )
    return jsonify({
        "reference": reference,
        "email_sent": email_sent,
        "message": "Cita registrada y confirmación enviada." if email_sent else
                   "Cita registrada. La confirmación por correo está pendiente de configurar.",
    }), 201


@app.post("/api/newsletter")
def subscribe_newsletter():
    data = json_payload()
    email = clean_text(data.get("email"), 254) if data else ""
    if not email_valid(email):
        return api_error("Escribe un email válido.", 400)
    if data.get("consent") is not True:
        return api_error("Necesitas aceptar las comunicaciones comerciales para suscribirte.", 400)
    try:
        with connect_db() as db:
            db.execute("INSERT INTO subscribers (email) VALUES (?)", (email.lower(),))
    except sqlite3.IntegrityError:
        return jsonify({"message": "Ese email ya está suscrito."})
    email_sent = send_email(
        email, f"Suscripción confirmada - {HOTEL_NAME}",
        f"Gracias por suscribirte a las novedades de {HOTEL_NAME}."
    )
    notify_hotel("Nueva suscripción a la newsletter", f"Email: {email.lower()}", reply_to=email)
    return jsonify({
        "message": "Suscripción confirmada." if email_sent else
                   "Suscripción guardada correctamente.",
        "email_sent": email_sent,
    }), 201

@app.post("/api/newsletter/unsubscribe")
def unsubscribe_newsletter():
    data = json_payload()
    email = clean_text(data.get("email"), 254).lower() if data else ""
    if not email_valid(email):
        return api_error("Escribe un email válido.", 400)
    with connect_db() as db:
        removed = db.execute("DELETE FROM subscribers WHERE email = ?", (email,)).rowcount > 0
    if removed:
        send_async(email, f"Baja confirmada - {HOTEL_NAME}",
                   f"Te has dado de baja de las novedades de {HOTEL_NAME}. No recibirás más correos comerciales.")
        notify_hotel("Baja de la newsletter", f"Email: {email}")
    return jsonify({"message": "Baja realizada." if removed else "Ese email no estaba suscrito."})

def _datos_dinamicos():
    precios = "\n".join(
        f"- {nombre}: desde {datos['price']} EUR por habitacion y noche."
        for nombre, datos in ROOMS.items()
    )
    nombres = {"spa": "Spa", "restaurant": "Restaurante", "activity": "Actividades"}
    horarios = "\n".join(
        f"- {nombres[k]} (reservable en la web): {', '.join(v)}."
        for k, v in SERVICE_TIMES.items()
    )
    return precios, horarios


_PRECIOS, _HORARIOS = _datos_dinamicos()

CONTEXTO_HOTEL = """
Eres el recepcionista virtual del hotel La Palma Princess (Hotel Princess La Palma).
Responde en el idioma del cliente, con tono amable y respuestas breves.

REGLAS ESTRICTAS:
- Responde SOLO con la información de este texto.
- Antes de decir que no tienes un dato, revisa TODO el texto de abajo. Solo si de verdad
  no aparece, di que no lo tienes y sugiere contactar con el hotel por teléfono, email o WhatsApp.
- Nunca inventes precios, horarios, servicios ni condiciones.
- No puedes hacer reservas: indica que usen la página de Reservas.
- Si preguntan algo que no tiene que ver con el hotel, di que solo puedes ayudar con temas del hotel.

INFORMACIÓN OFICIAL:
- Hotel de 4 estrellas en Fuencaliente, al sur de La Palma, sobre un acantilado con vistas al Atlántico.
  Habitaciones de estilo colonial en edificios de baja altura rodeados de jardines y palmeras.
- Dirección: Carretera de la Costa, Cerca Vieja, 10, 38749 Fuencaliente de La Palma.
- Teléfono: +34 922 42 55 11 (lunes a viernes, de 09:00 a 17:00).
- WhatsApp: 613 48 90 55.
- Email: {EMAIL}.

ZONAS:
- Zona general (familiar): siete piscinas exteriores, con piscina infantil, playa de arena artificial y solárium.
- Esencia de La Palma: zona exclusiva para adultos (+16), con cuatro piscinas, una climatizada en invierno.

PRECIOS (por noche, segun la pagina de Reservas):
{PRECIOS}
- Suplemento por adulto extra (mas de 2 por habitacion): 30 EUR/noche. Por nino: 12 EUR/noche.
- Descuento de residente: 10%. Codigo promocional: 5%.

HABITACIONES:
- Estándar: 22 m², estilo colonial, cama king size o dos individuales, sofá, baño con bañera y ducha,
  aire acondicionado y terraza o balcón. Hasta 3 adultos o 2 adultos y 2 niños.
- Familiar Superior: 44 m², dos habitaciones Estándar comunicadas, dos baños, vistas al jardín.
  Hasta 4 adultos y 2 niños.
- Ocean Suite: 44 m², salón y dormitorio independientes, dos baños, dos terrazas o balcones.
  Hasta 3 adultos y 2 niños.

SERVICIOS:
- Parking gratuito para huéspedes durante toda la estancia.
- Gimnasio con equipamiento de cardio y musculación, para huéspedes.
- Aqua Princess Spa: circuito de aguas, masajes y tratamientos. Recomendable reservar con antelación.
- Terrazas con vistas al Atlántico, rodeadas de jardines.
- Actividades y deportes para todas las edades.
- Servicio para niños: espacios y actividades infantiles y piscina infantil.
- Restaurantes con bufé, bar piscina, piano bar y un restaurante con menú de degustación.

RESERVAS:
- Se hacen en la página de Reservas. Responden por email en menos de 24 horas.
- Se pueden reservar hasta 4 habitaciones. Hay campo para código promocional (6 dígitos) y opción de residente.
- Adultos: desde 12 años. Niños: hasta 11 años.

SERVICIOS RESERVABLES EN LA WEB (horas disponibles):
{HORARIOS}
""".replace("{PRECIOS}", _PRECIOS).replace("{HORARIOS}", _HORARIOS).replace("{EMAIL}", HOTEL_EMAIL)


def _error_temporal(exc):
    t = str(exc).upper()
    return any(c in t for c in ("429", "500", "503", "RESOURCE_EXHAUSTED", "UNAVAILABLE", "OVERLOADED", "TIMEOUT"))


@app.route('/chat', methods=['POST'])
def chat():
    try:
        datos = request.get_json(silent=True)
        if not datos or "mensaje" not in datos:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400

        pregunta_usuario = str(datos.get("mensaje", "")).strip()[:1000]
        idioma_detectado = str(datos.get("idioma", "es")).strip()[:10]

        if not pregunta_usuario:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400
        if not client:
            app.logger.error("Chat sin cliente Gemini: falta GEMINI_API_KEY o el paquete google-genai")
            return jsonify({"respuesta": "El asistente virtual no está configurado.", "error": "sin_configurar"}), 503

        consulta_con_idioma = f"[Responder obligatoriamente en el idioma con código ISO: '{idioma_detectado}']\nPregunta: {pregunta_usuario}"

        for modelo_nombre in MODELOS_IA:
            for intento in range(2):
                try:
                    response = client.models.generate_content(
                        model=modelo_nombre,
                        contents=consulta_con_idioma,
                        config=types.GenerateContentConfig(
                            system_instruction=CONTEXTO_HOTEL,
                            temperature=0.3,
                        ),
                    )
                    texto = (response.text or "").strip() if response else ""
                    if texto:
                        app.logger.info("CHAT OK -> respondió la IA con el modelo %s | pregunta: %.80s", modelo_nombre, pregunta_usuario)
                        return jsonify({"respuesta": texto, "modelo": modelo_nombre})
                    app.logger.warning("Modelo %s devolvió respuesta vacía", modelo_nombre)
                    break
                except Exception as e_modelo:
                    # error (no warning) para que se vea claro en los logs de Render
                    app.logger.error("Fallo con modelo %s (intento %s): %s", modelo_nombre, intento + 1, e_modelo)
                    if intento == 0 and _error_temporal(e_modelo):
                        time.sleep(1.5)
                        continue
                    break

        app.logger.error("CHAT FALLO -> ningún modelo respondió (probados: %s)", ", ".join(MODELOS_IA))
        # Si llegamos aquí NO es que falte el dato: es que la IA no ha respondido.
        return jsonify({
            "respuesta": "El asistente está saturado en este momento. Inténtalo de nuevo en unos segundos.",
            "error": "ia_no_disponible",
        }), 503

    except Exception:
        app.logger.exception("Error general en endpoint /chat")
        return jsonify({"respuesta": "Lo siento, ha ocurrido un error. Vuelve a intentarlo en unos segundos.", "error": "interno"}), 500


@app.get("/api/chat-debug")
def chat_debug():
    """Solo si pones CHAT_DEBUG=1 en Render. Muestra que modelos ve tu API key."""
    if os.getenv("CHAT_DEBUG") != "1":
        return api_error("No disponible.", 404)
    if not client:
        return jsonify({"cliente": False, "modelos_configurados": MODELOS_IA})
    try:
        disponibles = sorted(m.name.replace("models/", "") for m in client.models.list())
    except Exception as e:
        return jsonify({"cliente": True, "error": str(e), "modelos_configurados": MODELOS_IA}), 500
    return jsonify({
        "modelos_configurados": MODELOS_IA,
        "configurados_que_existen": [m for m in MODELOS_IA if m in disponibles],
        "disponibles": [m for m in disponibles if "gemini" in m],
    })



# ───────────────────────── Correos sueltos (una sola definición de cada ruta) ─────────────────────────
@app.route('/enviar-email-promocional', methods=['POST'])
def enviar_email_promocional():
    datos = request.get_json(silent=True) or {}
    email_destino = clean_text(datos.get("email"), 254)

    if not email_destino or not email_valid(email_destino):
        return jsonify({"error": "Hace falta especificar un email válido"}), 400

    try:
        ruta_plantilla = Path('email_marketing.html')
        contenido_html = ruta_plantilla.read_text(encoding='utf-8') if ruta_plantilla.exists() else "<h1>Oferta 10% Dto</h1>"
        contenido_html = contenido_html.replace('{{email}}', escape(email_destino))
        if send_email(email_destino, "¡Oferta especial 10% Dto! - Hotel Princess La Palma", contenido_html):
            return jsonify({"mensaje": f"Correo promocional enviado a {email_destino} correctamente."}), 200
        return jsonify({"error": "No se pudo enviar el correo promocional."}), 500
    except Exception:
        app.logger.exception("Error en /enviar-email-promocional")
        return jsonify({"error": "Error procesando la plantilla del correo."}), 500


@app.route('/api/email-bienvenida', methods=['POST'])
def email_bienvenida():
    datos = request.get_json(silent=True) or {}
    nombre = escape(clean_text(datos.get('nombre', 'Usuario'), 100))
    email = clean_text(datos.get('email'), 254)

    if not email or not email_valid(email):
        return jsonify({"error": "Hace falta un email válido"}), 400

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">¡Bienvenido/a a La Palma Princess!</h1>
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Gracias por registrarte en nuestra plataforma. Tu cuenta ha sido creada correctamente.</p>
            <p>Ahora podrás realizar tus reservas de forma más rápida y acceder a ofertas exclusivas para tu estancia en Fuencaliente.</p>
            <hr style="border: none; border-top: 1px solid #eeeeee; margin: 20px 0;">
            <p style="font-size: 12px; color: #777;">
                Si deseas dejar de recibir correos comerciales, puedes
                <a href="{PUBLIC_BASE_URL}/desuscrito.html?email={escape(email)}">darse de baja aquí</a>.
            </p>
        </div>
    </div>
    """
    enviado = send_email(email, "¡Bienvenido/a a La Palma Princess!", html)
    
    # NUEVO: Te envía un aviso a ti como propietario (usa tu variable HOTEL_EMAIL)
    html_propietario = f"<h1>📢 Nuevo Registro</h1><p>El usuario <b>{nombre}</b> ({email}) se ha dado de alta en la web.</p>"
    send_email(HOTEL_EMAIL, "📢 Nuevo registro de usuario en la web", html_propietario)

    return jsonify({"mensaje": "Correo enviado correctamente" if enviado else "Usuario registrado, pero falló el envío de correo",
                    "email_sent": enviado}), 200


@app.route('/api/email-reserva', methods=['POST'])
def email_reserva():
    datos = request.get_json(silent=True) or {}
    nombre = escape(clean_text(datos.get('nombre', 'Huésped'), 100))
    email = clean_text(datos.get('email'), 254)
    checkin = escape(clean_text(datos.get('checkin'), 20))
    checkout = escape(clean_text(datos.get('checkout'), 20))
    habitacion = escape(clean_text(datos.get('habitacion'), 40))
    num_habitaciones = escape(clean_text(datos.get('num_habitaciones', 1), 3))
    adultos = escape(clean_text(datos.get('adultos', 1), 3))
    ninos = escape(clean_text(datos.get('ninos', 0), 3))

    if not email or not email_valid(email):
        return jsonify({"error": "Hace falta un email válido"}), 400

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">Confirmación de Reserva</h1>
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Hemos recibido tu solicitud de reserva en el <strong>Hotel Princess La Palma</strong>. A continuación detallamos los datos de tu estancia:</p>

            <table style="width: 100%; border-collapse: collapse; margin: 20px 0; background: #f8fafb; border: 1px solid #e2e8f0;">
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Fecha de entrada:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{checkin}</td></tr>
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Fecha de salida:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{checkout}</td></tr>
                <tr><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;"><strong>Tipo de habitación:</strong></td><td style="padding: 10px; border-bottom: 1px solid #e2e8f0;">{habitacion} ({num_habitaciones} hab.)</td></tr>
                <tr><td style="padding: 10px;"><strong>Huéspedes:</strong></td><td style="padding: 10px;">{adultos} adulto(s), {ninos} niño(s)</td></tr>
            </table>

            <p>Te responderemos por email en menos de 24 hours para formalizar el proceso.</p>
            <hr style="border: none; border-top: 1px solid #eeeeee; margin: 20px 0;">
            <p style="font-size: 12px; color: #777;">
                Si deseas cancelar tu suscripción o dejar de recibir estos avisos, haz clic en
                <a href="{PUBLIC_BASE_URL}/desuscrito.html?email={escape(email)}">darse de baja</a>.
            </p>
        </div>
    </div>
    """
    enviado = send_email(email, "Confirmación de tu reserva - Hotel Princess La Palma", html)
    
    # NUEVO: Te envía un aviso detallado a ti como propietario (usa tu variable HOTEL_EMAIL)
    html_propietario = f"""
    <h1>🚨 NUEVA RESERVA EN LA WEB</h1>
    <p>Un cliente ha completado una solicitud de reserva:</p>
    <ul>
        <li><b>Cliente:</b> {nombre} ({email})</li>
        <li><b>Habitación:</b> {habitacion} ({num_habitaciones} habs.)</li>
        <li><b>Estancia:</b> del {checkin} al {checkout}</li>
        <li><b>Huéspedes:</b> {adultos} Adultos, {ninos} Niños</li>
    </ul>
    """
    send_email(HOTEL_EMAIL, "🚨 Nueva Solicitud de Reserva Recibida", html_propietario)

    return jsonify({"mensaje": "Correo enviado correctamente" if enviado else "Reserva registrada, pero falló el envío de correo",
                    "email_sent": enviado}), 200

# ───────────────────────── Panel de administración (lista de registros) ─────────────────────────
# Solo funciona si en Render pones la variable ADMIN_KEY. Se abre en: /admin?clave=TU_CLAVE
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()


def _tabla_html(titulo, cabeceras, filas):
    cab = "".join(f"<th>{escape(c)}</th>" for c in cabeceras)
    cuerpo = "".join(
        "<tr>" + "".join(f"<td>{escape(str(v if v is not None else ''))}</td>" for v in fila) + "</tr>"
        for fila in filas
    ) or f'<tr><td colspan="{len(cabeceras)}">Sin datos</td></tr>'
    return f"<h2>{escape(titulo)} ({len(filas)})</h2><table><tr>{cab}</tr>{cuerpo}</table>"


@app.get("/admin")
def admin_panel():
    if not ADMIN_KEY:
        return api_error("No disponible.", 404)
    ip_key = "admin|" + (request.remote_addr or "")
    if attempts_count(ip_key, LOGIN_WINDOW) >= MAX_LOGIN_FAILS:
        return api_error("Demasiados intentos. Espera unos minutos.", 429)
    if not secrets.compare_digest(request.args.get("clave", "").encode(), ADMIN_KEY.encode()):
        attempts_add(ip_key)
        return api_error("No disponible.", 404)
    with connect_db() as db:
        usuarios = db.execute("SELECT name, email, created_at FROM users ORDER BY id DESC").fetchall()
        reservas = db.execute(
            "SELECT reference, name, email, room, rooms, checkin, checkout, total, status "
            "FROM bookings ORDER BY id DESC LIMIT 200").fetchall()
        servicios = db.execute(
            "SELECT reference, name, email, service, service_date, service_time, guests "
            "FROM service_bookings ORDER BY id DESC LIMIT 200").fetchall()
        suscriptores = db.execute("SELECT email, created_at FROM subscribers ORDER BY id DESC").fetchall()
    registrados = ", ".join(escape(u["name"]) for u in usuarios) or "nadie todavía"
    pagina = (
        '<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="robots" content="noindex">'
        '<meta name="viewport" content="width=device-width, initial-scale=1"><title>Panel</title>'
        '<style>body{font-family:Arial,sans-serif;margin:24px;color:#12303a}'
        'table{border-collapse:collapse;width:100%;margin-bottom:24px;font-size:14px}'
        'th,td{border:1px solid #cfd8dc;padding:6px 8px;text-align:left}th{background:#12303a;color:#fff}</style>'
        '</head><body><h1>La Palma Princess · Panel</h1>'
        f'<p><strong>Registrados ({len(usuarios)}):</strong> {registrados}</p>'
        + _tabla_html("Cuentas", ["Nombre", "Email", "Alta (UTC)"], usuarios)
        + _tabla_html("Reservas de habitación", ["Ref.", "Nombre", "Email", "Habitación", "Hab.", "Entrada", "Salida", "Total", "Estado"], reservas)
        + _tabla_html("Reservas de servicios", ["Ref.", "Nombre", "Email", "Servicio", "Fecha", "Hora", "Personas"], servicios)
        + _tabla_html("Suscriptores", ["Email", "Alta (UTC)"], suscriptores)
        + '</body></html>'
    )
    return pagina, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store",
                         "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
