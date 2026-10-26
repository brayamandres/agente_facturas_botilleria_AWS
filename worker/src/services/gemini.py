"""
Servicio de Extracción Multimodal con Google Gemini Vision.
Optimiza la imagen con Pillow y extrae encabezado y detalle de la factura en formato estructurado.
"""
import json
import logging
from decimal import Decimal
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
  "rut_proveedor": null,
  "fecha_emision": null,
  "moneda": null,
  "monto_neto": null,
  "monto_impuestos": null,
  "monto_iva": null,
  "impuestos_adicionales": null,
  "monto_exento": null,
  "total_factura": null,
  "items": [
    {
      "nombre_glosa": "Glosa o descripción original del ítem",
      "tipo_item": "PRODUCTO",
      "formato": "Formato, calibre o empaque si existe (ej: 750cc, Pack x6) o null",
      "cantidad": 0,
      "unidades": 0,
      "precio_unitario": 0,
      "base_precio": null,
      "monto_linea_neto": null
    }
  ]
}

Restricciones:
- No inventes ni asumas datos ausentes (usa null).
- Conserva la glosa original del proveedor sin normalizar ni corregir ortografía.
- Devuelve únicamente el objeto JSON sin explicaciones adicionales.
- Conserva los fletes, transporte y despacho con tipo_item CARGO; los productos usan PRODUCTO.
- Extrae fecha_emision en YYYY-MM-DD y moneda ISO de tres letras (CLP, USD, etc.) solo si puede identificarse.
- Extrae RUT del proveedor y montos del resumen de la factura, no del total de una línea.
- Los montos son números sin separadores de miles. No calcules montos ausentes: usa null.
- Usa cero solo si el documento lo indica. monto_impuestos es el TOTAL de impuestos, no solamente el IVA.
- Lee por separado monto_iva y cada línea IMP.ADIC., ILA u otro impuesto del resumen.
- impuestos_adicionales es una lista de objetos {"nombre": "IMP.ADIC. COD. 27", "codigo": "27", "tasa": "10%", "monto": 4912}.
- Incluye TODAS las líneas de impuestos adicionales aunque sus códigos se repitan. No los confundas con productos.
- Si no hay impuestos adicionales y puedes confirmarlo, usa []; si no puedes leer el desglose, usa null.
- No calcules impuestos usando tasas ni restando neto al total. El sistema sumará IVA y montos adicionales leídos.
- Conserva el total final del documento incluyendo cargos y descuentos; no lo reconstruyas desde los ítems.
- En el campo 'unidades', indica cuántas unidades vienen en caso de que se trate de un pack o bulto (si es unitario, 1).
- Extrae monto_linea_neto del subtotal neto impreso de cada línea, después de descuentos de línea. Si no es legible o no está, usa null; no lo calcules ni repartas totales del encabezado.
- base_precio indica si precio_unitario corresponde a UNIDAD individual o BULTO/pack. Usa UNIDAD o BULTO solo si se puede determinar del documento; de otro modo null.
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


def preprocesar_imagen_factura(ruta_imagen, max_size=(2048, 3072)):
    """
    Optimiza la imagen de la factura redimensionándola manteniendo el aspect ratio
    para reducir consumo de ancho de banda y latencia sin perder legibilidad OCR.
    """
    ruta_original = Path(ruta_imagen)
    if not ruta_original.exists():
        raise FileNotFoundError(f"No se encontró la imagen de factura en: {ruta_original}")

    img = Image.open(ruta_original)
    # Conserva resolución suficiente para el resumen financiero y sus importes pequeños.
    img.thumbnail(max_size)

    ruta_salida = DATA_OPTIMIZADO_DIR / f"opt_{ruta_original.name}"
    img.save(ruta_salida)
    return ruta_salida


def extraer_datos_factura(ruta_imagen):
    """
    Lee visualmente la factura con Gemini y devuelve los datos estructurados en un diccionario.
    """
    ruta_optimizada = preprocesar_imagen_factura(ruta_imagen)
    try:
        with Image.open(ruta_optimizada) as img:
            imagen = img.copy()
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
                return json.loads(respuesta.text, parse_float=Decimal)
            except Exception as e:
                err_msg = str(e)
                es_transitorio = any(c in err_msg for c in ["503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "quota"])
                if es_transitorio:
                    logger.warning("Fallo transitorio en modelo Gemini '%s' (%s). Esperando 1.5s y probando siguiente modelo de respaldo...", modelo, err_msg)
                    import time
                    time.sleep(1.5)
                else:
                    logger.error("Error definitivo/de configuración en modelo Gemini '%s': %s", modelo, err_msg)

        raise RuntimeError("No fue posible extraer los datos de la factura con los modelos de Gemini configurados.")
    finally:
        if ruta_optimizada and Path(ruta_optimizada).exists():
            try:
                Path(ruta_optimizada).unlink()
            except Exception as ex_del:
                logger.warning("No fue posible eliminar la imagen optimizada '%s': %s", ruta_optimizada, ex_del)

