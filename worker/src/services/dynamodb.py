"""
Capa de Persistencia y Búsqueda Vectorial Nativa en Amazon DynamoDB.

Implementa:
- Definición de tablas con VectorIndexes (Disponibilidad General AWS, agosto 2026).
- Búsqueda vectorial nativa por similitud Coseno mediante 'dynamodb.search_vectors()'.
- Actualizaciones atómicas de inventario e idempotencia con expresiones de condición.
"""
import time
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


def esperar_indice_vectorial_activo(
    table_name: str,
    index_name: str,
    timeout: int = 300,
    poll_interval: int = 5
) -> bool:
    """
    Espera de forma activa (polling) hasta que la tabla y su índice vectorial estén en estado ACTIVE
    y el proceso de Backfilling haya concluido.
    """
    client = get_dynamodb_client()
    inicio = time.time()
    logger.info("Esperando a que el índice vectorial '%s' en tabla '%s' esté ACTIVE...", index_name, table_name)

    while time.time() - inicio < timeout:
        try:
            desc = client.describe_table(TableName=table_name).get("Table", {})
            table_status = desc.get("TableStatus")

            if table_status == "ACTIVE":
                vector_indexes = desc.get("VectorIndexes", [])
                target_index = next((idx for idx in vector_indexes if idx.get("IndexName") == index_name), None)

                if target_index:
                    index_status = target_index.get("IndexStatus")
                    backfilling = target_index.get("Backfilling", False)

                    if index_status == "ACTIVE" and not backfilling:
                        logger.info(
                            "Índice vectorial '%s' en tabla '%s' está ACTIVE y listo para consultas (demoró %.1fs).",
                            index_name, table_name, time.time() - inicio
                        )
                        return True
                    else:
                        logger.info(
                            "Índice vectorial '%s': estado=%s, backfilling=%s. Reintentando en %ds...",
                            index_name, index_status, backfilling, poll_interval
                        )
                else:
                    logger.warning("Índice vectorial '%s' aún no aparece en describe_table. Reintentando...", index_name)
            else:
                logger.info("Tabla '%s': estado=%s. Reintentando en %ds...", table_name, table_status, poll_interval)

        except Exception as e:
            logger.warning("Error transitorio consultando estado de tabla '%s': %s", table_name, e)

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Tiempo de espera agotado ({timeout}s) esperando activación del índice vectorial '{index_name}' en '{table_name}'."
    )


def crear_tablas_si_no_existen():
    """
    Provisiona las tablas DynamoDB requeridas si aún no existen.
    Crea la tabla de productos configurando nativamente VectorIndexes con métrica COSINE
    y espera a que el índice esté activo.
    Intenta crear ambas tablas de forma independiente y reporta todos los errores al final.
    """
    client = get_dynamodb_client()
    tablas_existentes = client.list_tables().get("TableNames", [])
    errores = []

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
            logger.info("Tabla '%s' creada. Esperando activación del índice vectorial...", DYNAMODB_TABLE_PRODUCTOS)
            esperar_indice_vectorial_activo(DYNAMODB_TABLE_PRODUCTOS, DYNAMODB_VECTOR_INDEX_NAME)
        except Exception as e:
            logger.error("Error al crear tabla '%s': %s", DYNAMODB_TABLE_PRODUCTOS, str(e))
            errores.append(f"Fallo en tabla de productos '{DYNAMODB_TABLE_PRODUCTOS}': {str(e)}")

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
            errores.append(f"Fallo en tabla de historial '{DYNAMODB_TABLE_HISTORIAL}': {str(e)}")

    if errores:
        raise RuntimeError("Errores durante el provisionamiento de tablas DynamoDB:\n" + "\n".join(errores))


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
        # SearchVector en Boto3 espera una lista de AttributeValues
        vector_attr = [{"N": str(float(val))} for val in vector_query]

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
        for res_item in response.get("SearchResults", []):
            raw_item = res_item.get("Item", {})
            data = deserializar_item(raw_item)
            sku = data.get("sku", "")

            # Score retornado directamente en cada SearchResult
            score_nativo = res_item.get("Score")
            if score_nativo is not None:
                similitud = float(score_nativo)
            else:
                logger.warning("No se encontró el campo 'Score' en el SearchResult para SKU '%s'.", sku)
                similitud = 0.0

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


def consultar_duplicidad_en_historial(
    folio: Optional[str],
    proveedor: Optional[str] = None,
    factura_id_actual: Optional[str] = None
) -> Dict[str, Any]:
    """
    Verifica en la tabla de historial de DynamoDB si una factura ya fue registrada por otra transacción.
    Excluye la transacción actual (factura_id_actual) para evitar falsos positivos y solo considera
    estados finales o en resolución.
    """
    folio_limpio = str(folio).strip() if folio is not None else ""
    if not folio_limpio or folio_limpio.lower() in ["none", "null", "s/f", "n/a", "pendiente_extraccion"]:
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
        if proveedor_limpio and proveedor_limpio.lower() not in ["none", "null", "desconocido", "n/a", "pendiente_extraccion"]:
            filtro = filtro & Attr("proveedor").eq(proveedor_limpio)

        # Excluir el propio registro en curso para no auto-detectarse como duplicado
        if factura_id_actual and str(factura_id_actual).strip():
            filtro = filtro & Attr("id").ne(str(factura_id_actual).strip())

        # Solo considerar registros que completaron o están en resolución de stock
        # (ignora intentos en análisis inicial o abortados previamente)
        filtro = filtro & Attr("estado").is_in(["COMPLETADO", "REQUIERE_REVISION_MANUAL", "COMPLETADO_CON_ERRORES"])

        items = []
        scan_kwargs: Dict[str, Any] = {"FilterExpression": filtro}
        while True:
            res = table.scan(**scan_kwargs)
            items.extend(res.get("Items", []))
            if items:
                break
            last_key = res.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

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
    Utiliza expresiones de actualización atómica e idempotencia con ConditionExpression.
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

        # Idempotencia previa (fast-path): si la operación/folio ya se aplicó a este SKU, no duplicar stock
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

        # 2. Expresión de actualización atómica con ConditionExpression contra condiciones de carrera
        now_iso = datetime.now(timezone.utc).isoformat()
        set_clauses = ["precio_costo = :costo", "actualizado_en = :now"]
        add_clauses = ["stock_actual :cant"]
        expr_values: Dict[str, Any] = {
            ":cant": int(cantidad),
            ":costo": Decimal(str(nuevo_costo)),
            ":now": now_iso
        }

        if nuevo_precio_venta is not None and float(nuevo_precio_venta) > 0:
            set_clauses.append("precio_venta = :pv")
            expr_values[":pv"] = Decimal(str(nuevo_precio_venta))

        update_kwargs: Dict[str, Any] = {
            "Key": {"sku": sku},
            "ReturnValues": "ALL_NEW"
        }

        if folio_limpio:
            add_clauses.append("folios_aplicados :folio_set")
            expr_values[":folio_set"] = {folio_limpio}
            expr_values[":folio_str"] = folio_limpio
            update_kwargs["ConditionExpression"] = "attribute_exists(sku) AND (attribute_not_exists(folios_aplicados) OR NOT contains(folios_aplicados, :folio_str))"
        else:
            update_kwargs["ConditionExpression"] = "attribute_exists(sku)"

        update_kwargs["UpdateExpression"] = f"SET {', '.join(set_clauses)} ADD {', '.join(add_clauses)}"
        update_kwargs["ExpressionAttributeValues"] = expr_values

        try:
            upd_res = table.update_item(**update_kwargs)
        except ClientError as ce:
            err_code = ce.response.get("Error", {}).get("Code")
            if err_code == "ConditionalCheckFailedException" and folio_limpio:
                res_actual = table.get_item(Key={"sku": sku})
                item_actual = res_actual.get("Item", item)
                stock_actual = int(item_actual.get("stock_actual", 0))
                return {
                    "exito": True,
                    "ya_aplicado": True,
                    "sku": sku,
                    "stock_anterior": stock_actual,
                    "nuevo_stock": stock_actual,
                    "mensaje": f"El folio '{folio_limpio}' ya fue aplicado concurrentemente a este SKU. Se omite incremento."
                }
            raise

        nuevo_item = upd_res.get("Attributes", {})
        nuevo_stock = int(nuevo_item.get("stock_actual", 0))
        stock_anterior = nuevo_stock - int(cantidad)

        return {
            "exito": True,
            "sku": sku,
            "stock_anterior": stock_anterior,
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
        table.put_item(Item=item, ConditionExpression="attribute_not_exists(sku)")
        return {"exito": True, "sku": sku, "mensaje": f"Producto con SKU '{sku}' registrado exitosamente."}
    except ClientError as ce:
        err_code = ce.response.get("Error", {}).get("Code")
        if err_code == "ConditionalCheckFailedException":
            folios_nuevos = set(item.get("folios_aplicados") or [])
            if folios_nuevos:
                res_exist = table.get_item(Key={"sku": sku})
                folios_exist = set(res_exist.get("Item", {}).get("folios_aplicados") or [])
                if folios_nuevos.issubset(folios_exist):
                    return {
                        "exito": True,
                        "ya_aplicado": True,
                        "sku": sku,
                        "mensaje": f"El producto con SKU '{sku}' ya fue creado por este folio previamente."
                    }
            return {"exito": False, "mensaje": f"El producto con SKU '{sku}' ya existe en el catálogo."}
        logger.error("Error al registrar nuevo SKU '%s' en DynamoDB: %s", sku, str(ce))
        return {"exito": False, "mensaje": str(ce)}
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
        id_item = item_sanitizado.get("id")
        if id_item:
            res_prev = table.get_item(Key={"id": id_item})
            prev_item = res_prev.get("Item")
            if prev_item:
                merged = dict(prev_item)
                merged.update(item_sanitizado)
                item_sanitizado = merged

        table.put_item(Item=item_sanitizado)
        return {"exito": True, "id": item_sanitizado.get("id")}
    except Exception as e:
        logger.error("Error al registrar auditoría en DynamoDB: %s", str(e))
        return {"exito": False, "error": str(e)}

