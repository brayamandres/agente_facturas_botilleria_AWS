"""
Servicio de Extracción Multimodal con Google Gemini Vision.
Optimiza la imagen con Pillow y extrae encabezado y detalle de la factura en formato estructurado.
"""
import json
import logging
from pathlib import Path
from PIL import Image
from google import genai
from google.genai import types

from worker.config import GOOGLE_API_KEY, DATA_OPTIMIZADO_DIR, DEFAULT_GEMINI_MODELS

logger = logging.getLogger("worker.services.gemini")

PROMPT_EXTRACCION = """Rol: Eres un asistente experto en lectura y extracción de datos de facturas de distribuidores de licores en Chile.

Tarea: Extrae el encabezado y el detalle de ítems comprados en la factura de la imagen.

Formato de salida (JSON estricto):
{
  "proveedor": "Nombre del proveedor o null",
  "numero_factura": "Número o folio de la factura o null",
  "items": [
    {
      "nombre_glosa": "Glosa o descripción original del ítem",
      "formato": "Formato, calibre o empaque si existe (ej: 750cc, Pack x6) o null",
      "cantidad": 0,
      "unidades": 0,
      "precio_unitario": 0
    }
  ]
}

Restricciones:
- No inventes ni asumas datos ausentes (usa null).
- Conserva la glosa original del proveedor sin normalizar ni corregir ortografía.
- Devuelve únicamente el objeto JSON sin explicaciones adicionales.
- Si alguno de los items corresponde a costo de transporte, flete o despacho, ignóralo.
- En el campo 'unidades', indica cuántas unidades vienen en caso de que se trate de un pack o bulto (si es unitario, 1).
"""

_gemini_client = None


def get_gemini_client():
    """Retorna la instancia singleton del cliente Google Gemini."""
    global _gemini_client
    if _gemini_client is None:
        if not GOOGLE_API_KEY:
            raise ValueError("GOOGLE_API_KEY no está configurada en las variables de entorno.")
        _gemini_client = genai.Client(api_key=GOOGLE_API_KEY)
    return _gemini_client


def preprocesar_imagen_factura(ruta_imagen, max_size=(1280, 720)):
    """
    Optimiza la imagen de la factura redimensionándola manteniendo el aspect ratio
    para reducir consumo de ancho de banda y latencia sin perder legibilidad OCR.
    """
    ruta_original = Path(ruta_imagen)
    if not ruta_original.exists():
        raise FileNotFoundError(f"No se encontró la imagen de factura en: {ruta_original}")

    img = Image.open(ruta_original)
    img.thumbnail(max_size)

    ruta_salida = DATA_OPTIMIZADO_DIR / f"opt_{ruta_original.name}"
    img.save(ruta_salida)
    return ruta_salida


def extraer_datos_factura(ruta_imagen):
    """
    Lee visualmente la factura con Gemini y devuelve los datos estructurados en un diccionario.
    """
    ruta_optimizada = preprocesar_imagen_factura(ruta_imagen)
    imagen = Image.open(ruta_optimizada)
    client = get_gemini_client()

    for modelo in DEFAULT_GEMINI_MODELS:
        try:
            logger.info("Invocando modelo Gemini '%s' para extracción visual...", modelo)
            respuesta = client.models.generate_content(
                model=modelo,
                contents=[PROMPT_EXTRACCION, imagen],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json"
                )
            )
            return json.loads(respuesta.text)
        except Exception as e:
            err_msg = str(e)
            es_transitorio = any(c in err_msg for c in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "quota"])
            if es_transitorio:
                logger.warning("Fallo transitorio en modelo Gemini '%s' (%s). Probando siguiente modelo de respaldo...", modelo, err_msg)
            else:
                logger.error("Error definitivo/de configuración en modelo Gemini '%s': %s", modelo, err_msg)

    raise RuntimeError("No fue posible extraer los datos de la factura con los modelos de Gemini configurados.")

