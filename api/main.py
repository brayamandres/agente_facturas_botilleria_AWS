"""
Microservicio API de Ingesta Web y Gestión para Facturas de Botillería.

Arquitectura:
- Patrón Microservicios: Desacoplado de la inferencia pesada (el Worker maneja LangGraph y Vision).
- Patrón Orientado a Eventos (Event-Driven): SQS + S3 + DynamoDB.
- Human-in-the-Loop (HITL): Recepción y despacho de resoluciones humanas vía SQS.
- Monitoreo en Tiempo Real: Consulta de bitácora y estado episódico en DynamoDB.
- Visualización de Tablas: Endpoints para explorar catálogo e historial.
"""
import json
import logging
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, status, Request, Body
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from api.config import (
    S3_BUCKET_NAME,
    SQS_QUEUE_URL,
    DYNAMODB_TABLE_PRODUCTOS,
    DYNAMODB_TABLE_HISTORIAL,
    get_s3_client,
    get_sqs_client,
    get_dynamodb_resource
)

# Configuración de Logging Estructurado para CloudWatch
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [API] %(name)s - %(message)s"
)
logger = logging.getLogger("api.main")

# Directorios de plantillas y archivos estáticos
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"
TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
STATIC_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(
    title="API Botillería AWS - Facturación Inteligente",
    description="Microservicio de Ingesta, Auditoría y Resolución Humana (HITL)",
    version="2.1.0"
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _sanitizar_para_dynamodb(val: Any) -> Any:
    """Convierte floats a Decimal recursivamente para escrituras válidas en DynamoDB."""
    if isinstance(val, float):
        return Decimal(str(val))
    elif isinstance(val, dict):
        return {k: _sanitizar_para_dynamodb(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [_sanitizar_para_dynamodb(elem) for elem in val]
    return val


def _deserializar_decimales(obj: Any) -> Any:
    """Convierte tipos Decimal de DynamoDB a tipos estándar de Python para serialización JSON."""
    if isinstance(obj, list):
        return [_deserializar_decimales(i) for i in obj]
    elif isinstance(obj, dict):
        return {k: _deserializar_decimales(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        return int(obj) if obj % 1 == 0 else float(obj)
    return obj


# ==============================================================================
# 1. RUTAS DE VISTAS HTML
# ==============================================================================

@app.get("/", response_class=HTMLResponse, summary="Página Principal - Procesamiento y Logs")
def index_view(request: Request):
    """Renderiza la interfaz principal con carga de factura, visor, terminal de logs y modal HITL."""
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/tablas", response_class=HTMLResponse, summary="Página de Tablas - Catálogo e Historial")
def tablas_view(request: Request):
    """Renderiza el visualizador amigable de tablas DynamoDB (Productos e Historial)."""
    return templates.TemplateResponse("tablas.html", {"request": request})


# ==============================================================================
# 2. HEALTH CHECK PARA APPLICATION LOAD BALANCER (ALB)
# ==============================================================================

@app.get("/health", status_code=status.HTTP_200_OK, summary="Health Check para AWS ALB")
def health_check():
    """Sonda de salud para ALB y ECS."""
    return {"status": "ok", "service": "api-botilleria", "timestamp": datetime.now(timezone.utc).isoformat()}


# ==============================================================================
# 3. ENDPOINTS DE FACTURACIÓN Y PROCESAMIENTO
# ==============================================================================

@app.post(
    "/facturas/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recepción asíncrona de facturas"
)
async def upload_factura(file: UploadFile = File(...)):
    """
    Recibe una imagen de factura, la persiste en Amazon S3,
    crea el registro de seguimiento en DynamoDB y publica el evento en SQS.
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nombre de archivo inválido o ausente."
        )

    factura_id = str(uuid.uuid4())
    logger.info("Factura recibida. ID: %s | Archivo: %s", factura_id, file.filename)

    extension = file.filename.split(".")[-1].lower() if "." in file.filename else "png"
    s3_key = f"raw/{factura_id}.{extension}"
    ahora_iso = datetime.now(timezone.utc).isoformat()
    hora_str = datetime.now().strftime("%H:%M:%S")

    try:
        # 1. Leer bytes de la imagen
        contenido = await file.read()
        if len(contenido) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo proporcionado está vacío."
            )

        # 2. Persistir en Amazon S3
        s3 = get_s3_client()
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=contenido,
            ContentType=file.content_type or "image/png",
            Metadata={
                "factura_id": factura_id,
                "nombre_original": file.filename,
                "fecha_recepcion": ahora_iso
            }
        )

        # 3. Inicializar registro de seguimiento en DynamoDB
        dynamo = get_dynamodb_resource()
        tabla_historial = dynamo.Table(DYNAMODB_TABLE_HISTORIAL)
        bitacora_inicial = [
            f"[{hora_str}] Factura '{file.filename}' recibida y almacenada en Amazon S3.",
            f"[{hora_str}] Evento encolado en AWS SQS para procesamiento asíncrono con IA."
        ]

        tabla_historial.put_item(
            Item={
                "id": factura_id,
                "folio": "PENDIENTE_EXTRACCION",
                "proveedor": "PENDIENTE_EXTRACCION",
                "nombre_original": file.filename,
                "s3_key": s3_key,
                "estado": "EN_COLA",
                "total_items": 0,
                "items": [],
                "bitacora": bitacora_inicial,
                "fecha_recepcion": ahora_iso,
                "fecha_procesamiento": ahora_iso
            }
        )

        # 4. Publicar evento en AWS SQS
        evento = {
            "tipo_evento": "NUEVA_FACTURA",
            "factura_id": factura_id,
            "s3_bucket": S3_BUCKET_NAME,
            "s3_key": s3_key,
            "nombre_original": file.filename,
            "timestamp": ahora_iso
        }

        if SQS_QUEUE_URL:
            sqs = get_sqs_client()
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps(evento)
            )

        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "factura_id": factura_id,
                "status": "ACEPTADO",
                "mensaje": "Factura recibida y encolada exitosamente para procesamiento asíncrono.",
                "s3_key": s3_key,
                "timestamp": ahora_iso
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error crítico durante la ingesta de la factura ID %s: %s", factura_id, str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo en almacenamiento o encolamiento: {str(e)}"
        )


@app.get(
    "/api/facturas/{factura_id}/estado",
    summary="Consulta en tiempo real del estado y bitácora de una factura"
)
def obtener_estado_factura(factura_id: str):
    """
    Retorna el estado de procesamiento, la bitácora de logs en vivo
    y los ítems pendientes de revisión humana (si aplica).
    """
    dynamo = get_dynamodb_resource()
    tabla = dynamo.Table(DYNAMODB_TABLE_HISTORIAL)

    try:
        res = tabla.get_item(Key={"id": factura_id})
        item = res.get("Item")
        if not item:
            # Buscar por si el ID se guardó con otro identificador secundario
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Factura no encontrada.")

        item_deserializado = _deserializar_decimales(item)

        # Detectar si hay ítems que requieran revisión humana
        items = item_deserializado.get("items", [])
        items_revision = [
            i for i in items
            if i.get("estado") == "REQUIERE_REVISION_MANUAL"
        ]

        return {
            "factura_id": factura_id,
            "estado": item_deserializado.get("estado", "DESCONOCIDO"),
            "folio": item_deserializado.get("folio", "N/A"),
            "proveedor": item_deserializado.get("proveedor", "N/A"),
            "s3_key": item_deserializado.get("s3_key", ""),
            "total_items": item_deserializado.get("total_items", len(items)),
            "bitacora": item_deserializado.get("bitacora", []),
            "items": items,
            "requiere_revision": len(items_revision) > 0,
            "items_revision": items_revision,
            "fecha_procesamiento": item_deserializado.get("fecha_procesamiento")
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error al consultar estado de factura %s: %s", factura_id, str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/api/facturas/{factura_id}/imagen-url",
    summary="Genera una URL prefirmada de Amazon S3 para visualizar la factura"
)
def obtener_url_imagen_factura(factura_id: str):
    """Genera una URL temporal prefirmada (expira en 1 hora) para visualizar la factura original."""
    dynamo = get_dynamodb_resource()
    tabla = dynamo.Table(DYNAMODB_TABLE_HISTORIAL)

    res = tabla.get_item(Key={"id": factura_id})
    item = res.get("Item")
    if not item or not item.get("s3_key"):
        raise HTTPException(status_code=404, detail="Factura o imagen no encontrada.")

    s3 = get_s3_client()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": item["s3_key"]},
            ExpiresIn=3600
        )
        return {"url": url}
    except Exception as e:
        logger.error("Error al generar URL prefirmada de S3: %s", str(e))
        raise HTTPException(status_code=500, detail="No se pudo generar la URL de la imagen.")


# ==============================================================================
# 4. RESOLUCIÓN HUMANA (HUMAN-IN-THE-LOOP - HITL)
# ==============================================================================

@app.post(
    "/api/facturas/{factura_id}/resolver-item",
    status_code=status.HTTP_200_OK,
    summary="Aplica la decisión humana tomada en la ventana emergente"
)
def resolver_item_humano(factura_id: str, payload: Dict[str, Any] = Body(...)):
    """
    Recibe la decisión humana (Crear nuevo producto, asociar a SKU o descartar),
    despacha el evento al Worker vía SQS para vectorización/actualización
    y actualiza la bitácora en DynamoDB.
    """
    accion = payload.get("accion")  # "CREAR_NUEVO", "ASOCIAR_EXISTENTE", "OMITIR"
    glosa = payload.get("glosa_original", "Ítem sin glosa")
    sku = payload.get("sku")
    hora_str = datetime.now().strftime("%H:%M:%S")

    # 1. Enviar evento al Worker por SQS
    sqs = get_sqs_client()
    mensaje_sqs = {
        "tipo_evento": "RESOLUCION_HUMANA",
        "factura_id": factura_id,
        "resolucion": payload,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }

    if SQS_QUEUE_URL:
        sqs.send_message(
            QueueUrl=SQS_QUEUE_URL,
            MessageBody=json.dumps(mensaje_sqs)
        )

    # 2. Registrar avance inmediato en la bitácora y estado provisional de los ítems en DynamoDB
    dynamo = get_dynamodb_resource()
    tabla = dynamo.Table(DYNAMODB_TABLE_HISTORIAL)

    mensaje_bitacora = f"[{hora_str}] [HUMAN-IN-THE-LOOP] Operador seleccionó: '{accion}' para '{glosa}' (SKU: {sku or 'N/A'})."
    try:
        res_factura = tabla.get_item(Key={"id": factura_id})
        item_factura = res_factura.get("Item", {})
        items = item_factura.get("items", [])
        for it in items:
            if it.get("glosa_original") == glosa or (it.get("estado") == "REQUIERE_REVISION_MANUAL" and not it.get("sku_homologado") and it.get("glosa_original") == glosa):
                it["estado"] = "RESUELTO_MANUAL" if accion != "OMITIR" else "OMITIDO_POR_HUMANO"
                it["sku_homologado"] = sku if accion != "OMITIR" else None

        pendientes = [i for i in items if i.get("estado") == "REQUIERE_REVISION_MANUAL"]
        nuevo_st = "REQUIERE_REVISION_MANUAL" if pendientes else "COMPLETADO"

        tabla.update_item(
            Key={"id": factura_id},
            UpdateExpression="SET bitacora = list_append(if_not_exists(bitacora, :vacia), :nuevo_log), #items = :items, #st = :nuevo_st",
            ExpressionAttributeNames={
                "#st": "estado",
                "#items": "items"
            },
            ExpressionAttributeValues={
                ":nuevo_log": [mensaje_bitacora],
                ":vacia": [],
                ":items": _sanitizar_para_dynamodb(items),
                ":nuevo_st": nuevo_st
            }
        )
        logger.info("Factura ID %s actualizada en DynamoDB: item '%s' resuelto como '%s'. Nuevo estado: %s", factura_id, glosa, accion, nuevo_st)
    except Exception as e:
        logger.warning("No se pudo actualizar bitacora inmediatamente en DynamoDB: %s", e)

    return {
        "status": "ACEPTADO",
        "mensaje": f"Resolución para '{glosa}' encolada exitosamente para aplicación transaccional.",
        "factura_id": factura_id
    }


# ==============================================================================
# 5. ENDPOINTS DE EXPLORACIÓN DE TABLAS DYNAMODB
# ==============================================================================

@app.get(
    "/api/tablas/productos",
    summary="Obtiene la lista completa de productos del catálogo (sin vectores pesados)"
)
def listar_productos_catalogo():
    """Retorna los productos en DynamoDB excluyendo el vector de 384 dimensiones para optimizar ancho de banda."""
    dynamo = get_dynamodb_resource()
    tabla = dynamo.Table(DYNAMODB_TABLE_PRODUCTOS)

    try:
        res = tabla.scan(
            ProjectionExpression="sku, nombre, marca, categoria, subcategoria, formato, precio_costo, precio_venta, stock_actual, stock_minimo, activo, creado_en, actualizado_en"
        )
        items = res.get("Items", [])
        return {
            "total": len(items),
            "productos": _deserializar_decimales(items)
        }
    except Exception as e:
        logger.error("Error al escanear tabla de productos: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))


@app.get(
    "/api/tablas/historial",
    summary="Obtiene el historial de todas las facturas procesadas"
)
def listar_historial_facturas():
    """Retorna todas las facturas procesadas o en proceso ordenadas por fecha reciente."""
    dynamo = get_dynamodb_resource()
    tabla = dynamo.Table(DYNAMODB_TABLE_HISTORIAL)

    try:
        res = tabla.scan()
        items = res.get("Items", [])
        items_limpios = _deserializar_decimales(items)

        for item in items_limpios:
            items_detalle = item.get("items", [])
            pendientes = [i for i in items_detalle if i.get("estado") == "REQUIERE_REVISION_MANUAL"]
            item["pendientes_count"] = len(pendientes)
            item["items_revision"] = pendientes

        # Ordenar por fecha_procesamiento o fecha_recepcion descendente
        items_limpios.sort(
            key=lambda x: str(x.get("fecha_procesamiento") or x.get("fecha_recepcion") or ""),
            reverse=True
        )

        return {
            "total": len(items_limpios),
            "historial": items_limpios
        }
    except Exception as e:
        logger.error("Error al escanear tabla de historial: %s", str(e))
        raise HTTPException(status_code=500, detail=str(e))
