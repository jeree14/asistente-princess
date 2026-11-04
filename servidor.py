import os
import re
import secrets
import sqlite3
import hashlib
import smtplib
import threading
import traceback
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, request, jsonify
from flask_cors import CORS
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# CORS total para evitar cualquier tipo de bloqueo con GitHub Pages
CORS(app)

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

api_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=api_key) if genai and api_key else None
HOTEL_NAME = "La Palma Princess"
HOTEL_EMAIL = os.getenv("HOTEL_EMAIL", "hotelprincesslapalmatrabajo@gmail.com")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
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

# MODELOS DE RESPALDO PARA EL CHATBOT
MODELOS_IA = MODELOS_IA = [
    'gemini-2.5-flash',
    'gemini-2.5-pro',
    'gemini-2.0-flash',
    'gemini-1.5-flash-8b'
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


def send_email(recipient, subject, body, html_body=None):
    host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    username = os.getenv("SMTP_USERNAME", "hotelprincesslapalmatrabajo@gmail.com")
    password = os.getenv("SMTP_PASSWORD")
    
    if not password:
        app.logger.warning("SMTP_PASSWORD no configurado.")
        return False

    message = MIMEMultipart("alternative") if html_body else EmailMessage()
    message["Subject"] = subject
    message["From"] = os.getenv("SMTP_FROM") or HOTEL_EMAIL
    message["To"] = recipient

    if html_body:
        message.attach(MIMEText(body, "plain"))
        message.attach(MIMEText(html_body, "html"))
    else:
        message.set_content(body)

    try:
        port = int(os.getenv("SMTP_PORT", "465"))
        with smtplib.SMTP_SSL(host, port, timeout=15) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
        return True
    except Exception:
        app.logger.exception("Error al enviar email a %s", recipient)
        return False


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


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


CONTEXTO_HOTEL = """
Eres el recepcionista virtual del hotel La Palma Princess (Hotel Princess La Palma).
Responde en el idioma del cliente, con tono amable y respuestas breves.

REGLAS ESTRICTAS:
- Responde SOLO con la información de este texto.
- Si el dato no aparece aquí (precios, horarios, políticas...), di claramente que no
  tienes ese dato y sugiere contactar con el hotel por teléfono, email o WhatsApp.
- Nunca inventes precios, horarios, servicios ni condiciones.
- No puedes hacer reservas: indica que usen la página de Reservas.
- Si preguntan algo que no tiene que ver con el hotel, di que solo puedes ayudar con temas del hotel.

INFORMACIÓN OFICIAL:
- Hotel de 4 estrellas en Fuencaliente, al sur de La Palma, sobre un acantilado con vistas al Atlántico.
  Habitaciones de estilo colonial en edificios de baja altura rodeados de jardines y palmeras.
- Dirección: Carretera de la Costa, Cerca Vieja, 10, 38749 Fuencaliente de La Palma.
- Teléfono: +34 922 42 55 11 (lunes a viernes, de 09:00 a 17:00).
- WhatsApp: 613 48 90 55.
- Email: hotelPrincessLaPalma@gmail.com.

ZONAS:
- Zona general (familiar): siete piscinas exteriores, con piscina infantil, playa de arena artificial y solárium.
- Esencia de La Palma: zona exclusiva para adultos (+16), con cuatro piscinas, una climatizada en invierno.

HABITACIONES (el precio no está publicado: se consulta al reservar):
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
"""


@app.route('/chat', methods=['POST'])
def chat():
    try:
        datos = request.get_json(silent=True)
        if not datos or "mensaje" not in datos:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400

        pregunta_usuario = str(datos.get("mensaje", "")).strip()
        idioma_detectado = str(datos.get("idioma", "es")).strip()

        if not pregunta_usuario:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400
        if not client:
            return jsonify({"respuesta": "El asistente virtual no está configurado (falta GEMINI_API_KEY en Render)."}), 503

        consulta_con_idioma = f"[Responder obligatoriamente en el idioma con código ISO: '{idioma_detectado}']\nPregunta: {pregunta_usuario}"

        # Recorre los modelos de respaldo (Gemini 2.5 y 2.0)
        for modelo_nombre in MODELOS_IA:
            try:
                response = client.models.generate_content(
                    model=modelo_nombre,
                    contents=consulta_con_idioma,
                    config=types.GenerateContentConfig(
                        system_instruction=CONTEXTO_HOTEL,
                        temperature=0.7,
                    ),
                )
                if response and response.text:
                    return jsonify({"respuesta": response.text})
            except Exception as e_modelo:
                app.logger.warning(f"Error en modelo {modelo_nombre}: {e_modelo}")
                continue

        return jsonify({"respuesta": "No tengo ese dato ahora mismo. Escríbenos por WhatsApp al 613 48 90 55 o llámanos al +34 922 42 55 11."}), 200

    except Exception:
        app.logger.exception("Error general en endpoint /chat")
        return jsonify({"respuesta": "Lo siento, ha ocurrido un error de conexión con la IA. Vuelve a intentarlo en unos segundos."}), 500


@app.route('/enviar-email-promocional', methods=['POST'])
def enviar_email_promocional():
    datos = request.get_json(silent=True) or {}
    email_destino = datos.get("email")

    if not email_destino or not email_valid(email_destino):
        return jsonify({"error": "Hace falta especificar un email válido"}), 400

    try:
        ruta_plantilla = Path('email_marketing.html')
        contenido_html = ruta_plantilla.read_text(encoding='utf-8') if ruta_plantilla.exists() else "<h1>Oferta 10% Dto</h1>"
        contenido_html = contenido_html.replace('{{email}}', email_destino)

        exito = send_email(
            recipient=email_destino,
            subject="¡Oferta especial 10% Dto! - Hotel Princess La Palma",
            body="¡Oferta especial 10% Dto en Hotel Princess La Palma!",
            html_body=contenido_html
        )
        if exito:
            return jsonify({"mensaje": f"Correo promocional enviado a {email_destino} correctamente."}), 200
        return jsonify({"error": "No se pudo enviar el correo promocional."}), 500
    except Exception as e:
        return jsonify({"error": f"Error procesando plantilla: {str(e)}"}), 500


@app.route('/api/email-bienvenida', methods=['POST'])
def email_bienvenida():
    datos = request.get_json(silent=True) or {}
    nombre = clean_text(datos.get('nombre', 'Usuario'), 100)
    email = clean_text(datos.get('email'), 254)

    if not email or not email_valid(email):
        return jsonify({"error": "Email inválido"}), 400

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">¡Bienvenido/a a La Palma Princess!</h1>
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Gracias por registrarte en nuestra plataforma. Tu cuenta ha sido creada correctamente.</p>
        </div>
    </div>
    """
    if send_email(email, "¡Bienvenido a La Palma Princess!", "Bienvenido a La Palma Princess", html):
        return jsonify({"mensaje": "Correo enviado correctamente"}), 200
    return jsonify({"error": "Falló el envío de correo"}), 500


@app.route('/api/email-reserva', methods=['POST'])
def email_reserva():
    datos = request.get_json(silent=True) or {}
    nombre = clean_text(datos.get('nombre', 'Huésped'), 100)
    email = clean_text(datos.get('email'), 254)
    checkin = datos.get('checkin')
    checkout = datos.get('checkout')
    habitacion = datos.get('habitacion')
    num_habitaciones = datos.get('num_habitaciones', 1)
    adultos = datos.get('adultos', 1)
    ninos = datos.get('ninos', 0)

    if not email or not email_valid(email):
        return jsonify({"error": "Hace falta un email válido"}), 400

    html = f"""
    <div style="font-family: Arial, sans-serif; padding: 20px; background-color: #f4f6f8;">
        <div style="max-width: 600px; margin: 0 auto; background: white; border-radius: 8px; padding: 30px; border-top: 5px solid #12303a;">
            <h1 style="color: #12303a; margin-top: 0;">Confirmación de Reserva</h1>
            <p>Hola <strong>{nombre}</strong>,</p>
            <p>Hemos recibido tu solicitud de reserva en el <strong>Hotel Princess La Palma</strong>:</p>
            <ul>
                <li><strong>Entrada:</strong> {checkin}</li>
                <li><strong>Salida:</strong> {checkout}</li>
                <li><strong>Habitación:</strong> {habitacion} ({num_habitaciones} hab.)</li>
                <li><strong>Huéspedes:</strong> {adultos} adulto(s), {ninos} niño(s)</li>
            </ul>
        </div>
    </div>
    """
    if send_email(email, "Confirmación de tu reserva - Hotel Princess La Palma", "Confirmación de reserva", html):
        return jsonify({"mensaje": "Correo enviado correctamente"}), 200
    return jsonify({"error": "Falló el envío de correo"}), 500


if __name__ == '__main__':
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
