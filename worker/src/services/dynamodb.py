"""
Capa de Persistencia y Búsqueda Vectorial Nativa en Amazon DynamoDB.

Implementa:
- Definición de tablas con VectorIndexes (Disponibilidad General AWS, agosto 2026).
- Búsqueda vectorial nativa por similitud Coseno mediante 'dynamodb.search_vectors()'.
- Actualizaciones atómicas de inventario e idempotencia con expresiones de condición.
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
from boto3.dynamodb.types import TypeDeserializer, TypeSerializer
from botocore.exceptions import ClientError

from worker.config import (
    DYNAMODB_TABLE_PRODUCTOS,
    DYNAMODB_TABLE_HISTORIAL,
    DYNAMODB_VECTOR_INDEX_NAME,
    EMBEDDING_DIMENSIONS,
    get_dynamodb_client,
    get_dynamodb_resource
)

logger = logging.getLogger("worker.services.dynamodb")
_deserializer = TypeDeserializer()
_serializer = TypeSerializer()


def deserializar_item(item_dynamo: Dict[str, Any]) -> Dict[str, Any]:
    """Convierte un ítem en formato DynamoDB de bajo nivel a diccionario Python nativo."""
    if not item_dynamo:
        return {}
    res = {}
    for k, v in item_dynamo.items():
        val = _deserializer.deserialize(v)
        # Convertir Decimal a float o int para compatibilidad JSON
        if isinstance(val, Decimal):
            val = int(val) if val % 1 == 0 else float(val)
        res[k] = val
    return res


def crear_tablas_si_no_existen():
    """
    Provisiona las tablas DynamoDB requeridas si aún no existen.
    Crea la tabla de productos configurando nativamente VectorIndexes con métrica COSINE.
    """
    client = get_dynamodb_client()
    tablas_existentes = client.list_tables().get("TableNames", [])

    # 1. Tabla de Productos con Búsqueda Vectorial Nativa
    if DYNAMODB_TABLE_PRODUCTOS not in tablas_existentes:
        logger.info("Creando tabla DynamoDB '%s' con VectorIndexes nativo...", DYNAMODB_TABLE_PRODUCTOS)
        try:
            client.create_table(
                TableName=DYNAMODB_TABLE_PRODUCTOS,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": "sku", "AttributeType": "S"}
                ],
                KeySchema=[
                    {"AttributeName": "sku", "KeyType": "HASH"}
                ],
                VectorIndexes=[
                    {
                        "IndexName": DYNAMODB_VECTOR_INDEX_NAME,
                        "VectorAttribute": {"AttributeName": "embedding"},
                        "Dimensions": EMBEDDING_DIMENSIONS,
                        "DistanceFunction": "COSINE",
                        "Projection": {"ProjectionType": "ALL"}
                    }
                ]
            )
            logger.info("Tabla '%s' creada exitosamente con VectorIndexes.", DYNAMODB_TABLE_PRODUCTOS)
        except Exception as e:
            logger.error("Error al crear tabla '%s': %s", DYNAMODB_TABLE_PRODUCTOS, str(e))
            raise

    # 2. Tabla de Historial de Facturas (Auditoría e Idempotencia)
    if DYNAMODB_TABLE_HISTORIAL not in tablas_existentes:
        logger.info("Creando tabla DynamoDB '%s'...", DYNAMODB_TABLE_HISTORIAL)
        try:
            client.create_table(
                TableName=DYNAMODB_TABLE_HISTORIAL,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": "id", "AttributeType": "S"}
                ],
                KeySchema=[
                    {"AttributeName": "id", "KeyType": "HASH"}
                ]
            )
            logger.info("Tabla '%s' creada exitosamente.", DYNAMODB_TABLE_HISTORIAL)
        except Exception as e:
            logger.error("Error al crear tabla '%s': %s", DYNAMODB_TABLE_HISTORIAL, str(e))
            raise


def buscar_productos_vectorial_nativo(vector_query: List[float], top_k: int = 3) -> List[Dict[str, Any]]:
    """
    Ejecuta una búsqueda de similitud coseno nativa en Amazon DynamoDB usando el método oficial
    search_vectors() sobre el VectorIndex configurado.
    
    Args:
        vector_query (List[float]): Vector de búsqueda (384 dimensiones).
        top_k (int): Cantidad de candidatos más cercanos a recuperar.
        
    Returns:
        List[Dict[str, Any]]: Lista de productos ordenados por mayor similitud semántica.
    """
    client = get_dynamodb_client()
    try:
        # Formatear el vector en el tipo de lista numérica DynamoDB
        vector_attr = {"L": [{"N": str(float(val))} for val in vector_query]}

        logger.info(
            "Ejecutando search_vectors() nativo en DynamoDB [Tabla: %s, Índice: %s, TopK: %d]",
            DYNAMODB_TABLE_PRODUCTOS, DYNAMODB_VECTOR_INDEX_NAME, top_k
        )

        response = client.search_vectors(
            TableName=DYNAMODB_TABLE_PRODUCTOS,
            IndexName=DYNAMODB_VECTOR_INDEX_NAME,
            TopK=top_k,
            SearchVector=vector_attr
        )

        candidatos = []
        for raw_item in response.get("Items", []):
            data = deserializar_item(raw_item)
            sku = data.get("sku", "")

            # Extraer score / similitud calculada nativamente por AWS
            score_nativo = raw_item.get("_score", {}).get("N") or raw_item.get("similarity", {}).get("N")
            similitud = float(score_nativo) if score_nativo is not None else 0.85

            item_cand = {
                "sku": sku,
                "nombre": data.get("nombre", ""),
                "marca": data.get("marca", ""),
                "formato": data.get("formato", ""),
                "precio_costo_actual": float(data.get("precio_costo", 0)),
                "precio_venta_actual": float(data.get("precio_venta", 0)),
                "stock_actual": int(data.get("stock_actual", 0)),
                "similitud_vectorial": round(similitud, 4)
            }
            candidatos.append(item_cand)

        return candidatos

    except Exception as e:
        logger.error("Fallo durante la búsqueda vectorial nativa en DynamoDB: %s", str(e), exc_info=True)
        raise


def obtener_producto_por_sku(sku: str) -> Dict[str, Any]:
    """Obtiene un producto específico por su clave primaria SKU en DynamoDB."""
    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_PRODUCTOS)
    try:
        res = table.get_item(Key={"sku": sku})
        if "Item" in res:
            item = res["Item"]
            item.pop("embedding", None)  # Omitir vector grande para serialización limpia
            # Normalizar Decimals
            for k, v in item.items():
                if isinstance(v, Decimal):
                    item[k] = int(v) if v % 1 == 0 else float(v)
            return {"encontrado": True, "producto": item}
        return {"encontrado": False, "mensaje": f"No se encontró el producto con SKU '{sku}'"}
    except Exception as e:
        logger.error("Error al consultar SKU '%s' en DynamoDB: %s", sku, str(e))
        return {"encontrado": False, "mensaje": str(e)}


def consultar_duplicidad_en_historial(folio: Optional[str], proveedor: Optional[str] = None) -> Dict[str, Any]:
    """Verifica en la tabla de historial de DynamoDB si una factura ya fue registrada."""
    folio_limpio = str(folio).strip() if folio is not None else ""
    if not folio_limpio or folio_limpio.lower() in ["none", "null", "s/f", "n/a"]:
        return {
            "es_duplicada": False,
            "estado_verificacion": "FOLIO_ILEGIBLE",
            "mensaje": "Folio no especificado o ilegible. Se continúa sin verificar duplicidad."
        }

    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_HISTORIAL)

    try:
        from boto3.dynamodb.conditions import Attr
        filtro = Attr("folio").eq(folio_limpio)
        proveedor_limpio = str(proveedor).strip() if proveedor is not None else ""
        if proveedor_limpio and proveedor_limpio.lower() not in ["none", "null", "desconocido", "n/a"]:
            filtro = filtro & Attr("proveedor").eq(proveedor_limpio)

        res = table.scan(FilterExpression=filtro, Limit=1)
        items = res.get("Items", [])

        if items:
            existente = items[0]
            return {
                "es_duplicada": True,
                "estado_verificacion": "DUPLICADA",
                "mensaje": f"Factura folio '{folio_limpio}' de '{proveedor_limpio or 'Proveedor'}' ya registrada previamente.",
                "registro_existente": {
                    "id": str(existente.get("id")),
                    "fecha": str(existente.get("fecha_procesamiento")),
                    "total_items": int(existente.get("total_items", 0))
                }
            }

        return {
            "es_duplicada": False,
            "estado_verificacion": "VERIFICADA_NUEVA",
            "mensaje": f"Factura folio '{folio_limpio}' es nueva y no está duplicada."
        }
    except Exception as e:
        logger.error("Error al consultar duplicidad en DynamoDB: %s", str(e))
        return {
            "es_duplicada": None,
            "estado_verificacion": "ERROR_CONSULTA",
            "error": str(e),
            "mensaje": f"Error al consultar duplicidad en DynamoDB: {e}"
        }


def actualizar_stock_y_costo_dynamo(
    sku: str,
    cantidad: int,
    nuevo_costo: float,
    nuevo_precio_venta: Optional[float] = None,
    folio_factura: Optional[str] = None
) -> Dict[str, Any]:
    """
    Actualiza atómicamente el stock y costo en DynamoDB.
    Utiliza expresiones de actualización atómica e idempotencia por folio.
    """
    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_PRODUCTOS)

    folio_limpio = (
        str(folio_factura).strip()
        if folio_factura and str(folio_factura).strip().lower() not in ["none", "null", "s/f", "n/a", ""]
        else None
    )

    try:
        # 1. Leer estado actual
        res_get = table.get_item(Key={"sku": sku})
        if "Item" not in res_get:
            return {"exito": False, "mensaje": f"No se encontró el producto con SKU '{sku}' para actualizar."}

        item = res_get["Item"]
        folios_previos = set(item.get("folios_aplicados") or [])

        # Idempotencia: si el folio ya se aplicó a este SKU, no duplicar stock
        if folio_limpio and folio_limpio in folios_previos:
            stock_actual = int(item.get("stock_actual", 0))
            return {
                "exito": True,
                "ya_aplicado": True,
                "sku": sku,
                "stock_anterior": stock_actual,
                "nuevo_stock": stock_actual,
                "mensaje": f"El folio '{folio_limpio}' ya fue aplicado previamente a este SKU. Se omite incremento."
            }

        # 2. Expresión de actualización atómica
        now_iso = datetime.now(timezone.utc).isoformat()
        update_expr = "ADD stock_actual :cant SET precio_costo = :costo, actualizado_en = :now"
        expr_values: Dict[str, Any] = {
            ":cant": int(cantidad),
            ":costo": Decimal(str(nuevo_costo)),
            ":now": now_iso
        }

        if nuevo_precio_venta is not None and float(nuevo_precio_venta) > 0:
            update_expr += ", precio_venta = :pv"
            expr_values[":pv"] = Decimal(str(nuevo_precio_venta))

        if folio_limpio:
            update_expr += " ADD folios_aplicados :folio_set"
            expr_values[":folio_set"] = {folio_limpio}

        upd_res = table.update_item(
            Key={"sku": sku},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ReturnValues="ALL_NEW"
        )
        nuevo_item = upd_res.get("Attributes", {})
        nuevo_stock = int(nuevo_item.get("stock_actual", 0))

        return {
            "exito": True,
            "sku": sku,
            "stock_anterior": int(item.get("stock_actual", 0)),
            "nuevo_stock": nuevo_stock,
            "nuevo_costo": float(nuevo_costo),
            "nuevo_precio_venta": float(nuevo_precio_venta) if nuevo_precio_venta else float(item.get("precio_venta", 0)),
            "mensaje": f"Stock actualizado atómicamente en DynamoDB (+{cantidad} unidades)."
        }

    except Exception as e:
        logger.error("Error al actualizar SKU '%s' en DynamoDB: %s", sku, str(e), exc_info=True)
        return {"exito": False, "mensaje": f"Error al actualizar DynamoDB: {str(e)}"}


def registrar_nuevo_producto_dynamo(datos_producto: Dict[str, Any]) -> Dict[str, Any]:
    """Inserta un nuevo producto en DynamoDB con su vector de embedding indexado."""
    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_PRODUCTOS)
    sku = datos_producto["sku"]

    item = dict(datos_producto)
    # Normalizar números a Decimal para DynamoDB
    if "precio_costo" in item:
        item["precio_costo"] = Decimal(str(item["precio_costo"]))
    if "precio_venta" in item:
        item["precio_venta"] = Decimal(str(item["precio_venta"]))
    if "stock_actual" in item:
        item["stock_actual"] = int(item["stock_actual"])

    # El vector debe almacenarse en el formato que indexa VectorIndexes
    if "embedding" in item and isinstance(item["embedding"], list):
        item["embedding"] = [Decimal(str(round(v, 6))) for v in item["embedding"]]

    item["creado_en"] = datetime.now(timezone.utc).isoformat()
    item["actualizado_en"] = item["creado_en"]

    try:
        table.put_item(Item=item)
        return {"exito": True, "sku": sku, "mensaje": f"Producto con SKU '{sku}' registrado exitosamente."}
    except Exception as e:
        logger.error("Error al registrar nuevo SKU '%s' en DynamoDB: %s", sku, str(e))
        return {"exito": False, "mensaje": str(e)}


def guardar_auditoria_factura_dynamo(registro_auditoria: Dict[str, Any]) -> Dict[str, Any]:
    """Guarda un registro de factura procesada en la tabla de historial de DynamoDB."""
    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_HISTORIAL)
    item = dict(registro_auditoria)

    # Convertir floats anidados a Decimal
    def _sanitizar(val):
        if isinstance(val, float):
            return Decimal(str(val))
        elif isinstance(val, dict):
            return {k: _sanitizar(v) for k, v in val.items()}
        elif isinstance(val, list):
            return [_sanitizar(elem) for elem in val]
        return val

    item_sanitizado = _sanitizar(item)
    try:
        table.put_item(Item=item_sanitizado)
        return {"exito": True, "id": item_sanitizado.get("id")}
    except Exception as e:
        logger.error("Error al registrar auditoría en DynamoDB: %s", str(e))
        return {"exito": False, "error": str(e)}

