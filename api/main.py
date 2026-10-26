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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, status, Request, Body
from fastapi.responses import JSONResponse, HTMLResponse, Response
from pydantic import BaseModel, Field
from api.services.estadisticas import scan_completo, calcular_estadisticas, exportar_csv, detalle_factura
from api.services.asistente import responder
from shared.finanzas import validar_finanzas
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


@app.get("/estadisticas", response_class=HTMLResponse)
def estadisticas_view(request: Request):
    return templates.TemplateResponse(request=request, name="estadisticas.html", context={})


def _historial_completo():
    return scan_completo(get_dynamodb_resource().Table(DYNAMODB_TABLE_HISTORIAL))


def _estadisticas(desde, hasta, moneda):
    try:
        return calcular_estadisticas(_historial_completo(), desde, hasta, moneda)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("No se pudo consultar estadísticas")
        raise HTTPException(status_code=503, detail="No se pudo consultar el historial. Revisa la conexión con DynamoDB.")


@app.get("/api/estadisticas")
def obtener_estadisticas(desde: date, hasta: date, moneda: str = "CLP"):
    moneda = moneda.strip().upper()
    if len(moneda) != 3 or not moneda.isascii() or not moneda.isalpha():
        raise HTTPException(status_code=422, detail="Moneda inválida.")
    return _deserializar_decimales(_estadisticas(desde.isoformat(), hasta.isoformat(), moneda))


@app.get("/api/facturas/{factura_id}/detalle")
def obtener_detalle_factura(factura_id: str):
    try:
        tabla = get_dynamodb_resource().Table(DYNAMODB_TABLE_HISTORIAL)
        factura = tabla.get_item(Key={"id": factura_id}, ConsistentRead=True).get("Item")
    except Exception:
        logger.exception("No se pudo consultar detalle de factura")
        raise HTTPException(status_code=503, detail="No fue posible consultar el detalle.")
    if not factura:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    return _deserializar_decimales(detalle_factura(factura))


@app.get("/api/estadisticas/exportar.csv")
def descargar_estadisticas(desde: date, hasta: date, moneda: str = "CLP"):
    if len(moneda) != 3 or not moneda.isascii() or not moneda.isalpha():
        raise HTTPException(status_code=422, detail="Moneda inválida.")
    datos = _estadisticas(desde.isoformat(), hasta.isoformat(), moneda.upper())
    return Response(exportar_csv(datos), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="facturas_{desde}_{hasta}.csv"'})


class PreguntaAsistente(BaseModel):
    pregunta: str = Field(min_length=3, max_length=1000)
    desde: date
    hasta: date
    moneda: str = Field(default="CLP", pattern=r"^[A-Z]{3}$")


@app.post("/api/asistente")
def consultar_asistente(payload: PreguntaAsistente):
    try:
        return _deserializar_decimales(responder(payload.pregunta, _historial_completo(), payload.desde.isoformat(), payload.hasta.isoformat(), payload.moneda))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception:
        logger.exception("Fallo en consulta del asistente")
        raise HTTPException(status_code=503, detail="No fue posible consultar los datos del asistente.")


class ImpuestoAdicional(BaseModel):
    nombre: str = Field(default="Impuesto adicional", max_length=200)
    codigo: str | None = Field(default=None, max_length=30)
    tasa: str | None = Field(default=None, max_length=30)
    monto: Decimal | None = Field(default=None, ge=0)


class EncabezadoFinanciero(BaseModel):
    proveedor: str = Field(min_length=1, max_length=200)
    fecha_emision: date | None = None
    rut_proveedor: str | None = Field(default=None, max_length=20)
    moneda: str | None = Field(default=None, max_length=3)
    monto_neto: Decimal | None = Field(default=None, ge=0)
    monto_impuestos: Decimal | None = Field(default=None, ge=0)
    monto_iva: Decimal | None = Field(default=None, ge=0)
    impuestos_adicionales: List[ImpuestoAdicional] | None = Field(default=None, max_length=100)
    monto_exento: Decimal | None = Field(default=None, ge=0)
    total_factura: Decimal | None = Field(default=None, ge=0)


@app.patch("/api/facturas/{factura_id}/finanzas")
def actualizar_finanzas(factura_id: str, payload: EncabezadoFinanciero):
    """Corrige solo el encabezado: no publica eventos ni modifica inventario."""
    tabla = get_dynamodb_resource().Table(DYNAMODB_TABLE_HISTORIAL)
    actual = tabla.get_item(Key={"id": factura_id}, ConsistentRead=True).get("Item")
    if not actual:
        raise HTTPException(status_code=404, detail="Factura no encontrada.")
    if actual.get("estado") not in {"COMPLETADO", "REQUIERE_REVISION_MANUAL", "COMPLETADO_CON_ERRORES"}:
        raise HTTPException(status_code=409, detail="Espera a que termine el procesamiento de la factura.")
    datos = payload.model_dump()
    datos["fecha_emision"] = payload.fecha_emision.isoformat() if payload.fecha_emision else None
    financieros = validar_finanzas(datos)
    campos = {**financieros, "proveedor": payload.proveedor.strip(), "finanzas_actualizadas_en": datetime.now(timezone.utc).isoformat()}
    nombres = {f"#c{i}": k for i, k in enumerate(campos)}
    valores = {f":v{i}": v for i, v in enumerate(campos.values())}
    nombres["#id"] = "id"
    nombres["#estado"] = "estado"
    valores[":estado_actual"] = actual["estado"]
    try:
        tabla.update_item(Key={"id": factura_id}, UpdateExpression="SET " + ", ".join(f"#c{i} = :v{i}" for i in range(len(campos))), ExpressionAttributeNames=nombres, ExpressionAttributeValues=valores, ConditionExpression="attribute_exists(#id) AND #estado = :estado_actual")
    except Exception as exc:
        if getattr(exc, "response", {}).get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise HTTPException(status_code=409, detail="La factura cambió. Actualiza antes de guardar.") from exc
        logger.exception("No se pudo guardar encabezado financiero")
        raise HTTPException(status_code=503, detail="No fue posible guardar los datos financieros.") from exc
    return _deserializar_decimales(campos)


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

        if not SQS_QUEUE_URL:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="SQS_QUEUE_URL no está configurada. No se pudo encolar la factura."
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
    s3_key = (item.get("s3_key") or item.get("ruta_imagen")) if item else None
    if not item or not s3_key:
        raise HTTPException(status_code=404, detail="Factura o imagen no encontrada.")

    s3 = get_s3_client()
    try:
        url = s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": S3_BUCKET_NAME, "Key": s3_key},
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
    if not SQS_QUEUE_URL:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="SQS_QUEUE_URL no está configurada. No se pudo encolar la resolución humana."
        )

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
        items = scan_completo(tabla,
            ProjectionExpression="sku, nombre, marca, categoria, subcategoria, formato, precio_costo, precio_venta, stock_actual, stock_minimo, activo, creado_en, actualizado_en"
        )
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
        items = scan_completo(tabla)
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
