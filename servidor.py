import os
import time
import traceback
from flask import Flask, request, jsonify
from flask_cors import CORS
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv(override=True)

app = Flask(__name__)

# Solo estas webs pueden usar el asistente (evita que otros gasten vuestra cuota)
CORS(app, origins=[
    "https://shouyi77.github.io",   # la web publicada (sin barra ni ruta al final)
    "http://127.0.0.1:5500",        # Live Server de VS Code
    "http://localhost:5500",
    "http://127.0.0.1:5000",
    "http://localhost:5000",
])

api_key = os.getenv("GEMINI_API_KEY")
if not api_key:
    raise ValueError("¡Error! No se ha podido leer GEMINI_API_KEY (archivo .env o variable de entorno).")

# Si Gemini no responde en 20 segundos, falla y muestra el error en la terminal
client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=12000))


def listar_modelos_alternativos():
    """Modelos 'flash' de tu clave, como reserva por si el principal está saturado."""
    try:
        nombres = []
        for m in client.models.list():
            nombre = m.name.replace("models/", "")
            acciones = getattr(m, "supported_actions", None) or ["generateContent"]
            if "flash" not in nombre or "generateContent" not in acciones:
                continue
            if any(x in nombre for x in ("image", "tts", "live", "audio", "embed", "robotics", "omni")):
                continue
            nombres.append(nombre)
        estables = [n for n in nombres if not any(x in n for x in ("preview", "exp"))]
        otros = [n for n in nombres if n not in estables]
        return sorted(estables, reverse=True) + sorted(otros, reverse=True)
    except Exception as e:
        print("No se pudo listar los modelos:", e)
        return []


# El principal es el original; si falla se prueban hasta 3 de reserva
PRINCIPAL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
MODELOS = [PRINCIPAL] + [m for m in listar_modelos_alternativos() if m != PRINCIPAL][:3]
print("Modelos que se probarán, en orden:", MODELOS)


def generar_respuesta(pregunta):
    """Prueba los modelos por orden; si uno está saturado o sin cuota, pasa al siguiente."""
    ultimo_error = None
    for modelo in list(MODELOS):
        for intento in range(2):
            try:
                r = client.models.generate_content(
                    model=modelo,
                    contents=pregunta,
                    config=types.GenerateContentConfig(
                        system_instruction=CONTEXTO_HOTEL,
                        temperature=0.7,
                    ),
                )
                texto = (r.text or "").strip()
                if texto:
                    if MODELOS[0] != modelo:          # recuerda el que funciona
                        MODELOS.remove(modelo)
                        MODELOS.insert(0, modelo)
                    print("Respondió el modelo:", modelo)
                    return texto
                break
            except Exception as e:
                ultimo_error = e
                if "503" in str(e) and intento == 0:
                    time.sleep(1.5)                   # un reintento si está saturado
                    continue
                print("El modelo", modelo, "falló:", str(e)[:200])
                break
    if ultimo_error:
        raise ultimo_error
    return ""

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


@app.route('/', methods=['GET'])
def inicio():
    # Sirve para comprobar que el servidor está despierto (y para "despertarlo" en Render)
    return jsonify({"estado": "ok", "servicio": "Asistente La Palma Princess"})


@app.route('/chat', methods=['POST'])
def chat():
    try:
        datos = request.get_json(silent=True)
        if not datos or "mensaje" not in datos:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400

        pregunta_usuario = str(datos.get("mensaje", "")).strip()
        if not pregunta_usuario:
            return jsonify({"respuesta": "No se ha recibido ningún mensaje."}), 400

        # Límite de longitud para que nadie envíe textos enormes
        pregunta_usuario = pregunta_usuario[:500]

        texto = generar_respuesta(pregunta_usuario)
        if not texto:
            texto = "No he podido generar una respuesta. Contacta con el hotel por WhatsApp: 613 48 90 55."
        return jsonify({"respuesta": texto})

    except Exception:
        print("--- ERROR DETALLADO EN EL SERVIDOR ---")
        traceback.print_exc()
        return jsonify({"respuesta": "Lo siento, ha ocurrido un error de conexión con la IA. Vuelve a intentarlo en unos segundos."}), 500


if __name__ == '__main__':
    # En local: python servidor.py  ->  http://127.0.0.1:5000
    # En Render se arranca con gunicorn (este bloque no se usa)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
