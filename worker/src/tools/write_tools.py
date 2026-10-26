"""
Herramientas de Escritura (Writing Tools) para el agente LangGraph.
Persisten cambios transaccionales en Amazon DynamoDB: actualización de stock,
alta de nuevos SKUs y auditoría histórica de facturas.
"""
import uuid
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool
from shared.finanzas import validar_finanzas

from worker.src.services.dynamodb import (
    obtener_producto_por_sku,
    actualizar_stock_y_costo_dynamo,
    registrar_nuevo_producto_dynamo,
    guardar_auditoria_factura_dynamo
)
from worker.src.services.embeddings import generar_embedding

logger = logging.getLogger("worker.tools.write_tools")


@tool
def actualizar_stock_y_costo(
    sku: str,
    cantidad: int,
    nuevo_costo: float,
    nuevo_precio_venta: Optional[float] = None,
    folio_factura: Optional[str] = None
) -> Dict[str, Any]:
    """
    Actualiza atómicamente el stock y costo unitario de un producto en Amazon DynamoDB.
    Aplica idempotencia por número de folio para evitar duplicidad de stock en reintentos.
    
    Args:
        sku (str): SKU del producto a actualizar.
        cantidad (int): Cantidad de unidades individuales a sumar al stock actual.
        nuevo_costo (float): Nuevo precio de costo unitario del producto.
        nuevo_precio_venta (float | None, optional): Nuevo precio de venta al público (opcional).
        folio_factura (str | None, optional): Folio de la factura para control de idempotencia.
        
    Returns:
        dict: Diccionario con el resultado de la operación, incluyendo nuevo stock y precios.
    """
    logger.info("Actualizando inventario en DynamoDB para SKU '%s' (+%d unidades)...", sku, cantidad)
    return actualizar_stock_y_costo_dynamo(
        sku=sku,
        cantidad=cantidad,
        nuevo_costo=nuevo_costo,
        nuevo_precio_venta=nuevo_precio_venta,
        folio_factura=folio_factura
    )


@tool
def registrar_nuevo_producto(
    sku: str,
    nombre: str,
    marca: str,
    categoria: str,
    subcategoria: str,
    formato: str,
    precio_costo: float,
    precio_venta: float,
    stock_inicial: int,
    stock_minimo: int = 6,
    folio_factura: Optional[str] = None
) -> Dict[str, Any]:
    """
    Da de alta un nuevo producto en DynamoDB calculando su vector de embedding de 384 dimensiones
    para que quede indexado inmediatamente en el VectorIndexes nativo de AWS.
    
    Args:
        sku (str): SKU único del producto.
        nombre (str): Nombre comercial del producto.
        marca (str): Marca del producto.
        categoria (str): Categoría del producto.
        subcategoria (str): Subcategoría del producto.
        formato (str): Formato del envase (ej: 750cc, Pack x6).
        precio_costo (float): Precio de costo unitario de compra.
        precio_venta (float): Precio de venta sugerido al público.
        stock_inicial (int): Stock inicial a ingresar.
        stock_minimo (int, optional): Umbral mínimo de stock. Default es 6.
        folio_factura (str | None, optional): Folio de la factura de origen.
        
    Returns:
        dict: Diccionario con el estado de la creación y confirmación del SKU.
    """
    logger.info("Registrando nuevo SKU '%s' en catálogo con vector nativo...", sku)

    folio_limpio = (
        str(folio_factura).strip()
        if folio_factura and str(folio_factura).strip().lower() not in ["none", "null", "s/f", "n/a", ""]
        else None
    )

    # Verificar si el SKU ya existe
    existente = obtener_producto_por_sku(sku)
    if existente.get("encontrado"):
        folios_existentes = set(existente.get("producto", {}).get("folios_aplicados") or [])
        if folio_limpio and folio_limpio in folios_existentes:
            return {
                "exito": True,
                "ya_aplicado": True,
                "sku": sku,
                "mensaje": f"El producto con SKU '{sku}' ya fue registrado por el folio '{folio_limpio}'."
            }
        return {
            "exito": False,
            "mensaje": f"El producto con SKU '{sku}' ya existe en el catálogo."
        }

    texto_descriptivo = f"{nombre} {marca} {categoria} {subcategoria} {formato}".strip()
    vector = generar_embedding(texto_descriptivo)

    datos_producto = {
        "sku": sku,
        "nombre": nombre,
        "marca": marca,
        "categoria": categoria,
        "subcategoria": subcategoria,
        "formato": formato,
        "precio_costo": precio_costo,
        "precio_venta": precio_venta,
        "stock_actual": stock_inicial,
        "stock_minimo": stock_minimo,
        "activo": True,
        "texto_descriptivo": texto_descriptivo,
        "embedding": vector
    }
    if folio_limpio:
        datos_producto["folios_aplicados"] = {folio_limpio}

    return registrar_nuevo_producto_dynamo(datos_producto)


@tool
def registrar_auditoria_factura(
    folio: str,
    proveedor: str,
    total_items: int,
    items_procesados: List[Dict[str, Any]],
    estado: str = "COMPLETADO",
    ruta_imagen: Optional[str] = None,
    s3_key: Optional[str] = None,
    total_factura: Optional[Decimal] = None,
    id_auditoria: Optional[str] = None,
    bitacora: Optional[List[str]] = None,
    datos_financieros: Optional[Dict[str, Any]] = None,
    cargos: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Registra el procesamiento completo de una factura en la tabla 'botilleria-historial-facturas'
    de Amazon DynamoDB como memoria episódica a largo plazo para auditoría y control de duplicados.
    
    Args:
        folio (str): Folio o número de la factura.
        proveedor (str): Nombre del proveedor emisor.
        total_items (int): Cantidad de ítems contenidos en la factura.
        items_procesados (list[dict]): Detalle de los ítems homologados y aplicados.
        estado (str, optional): Estado final ('COMPLETADO' o 'COMPLETADO_CON_ERRORES').
        ruta_imagen (str | None, optional): Identificador de la imagen o S3 key.
        s3_key (str | None, optional): Clave del objeto en Amazon S3 para el visor web.
        total_factura (float | None, optional): Monto total de la factura.
        id_auditoria (str | None, optional): ID único de seguimiento creado en la ingesta.
        bitacora (list[str] | None, optional): Registro cronológico de ejecución.
        
    Returns:
        dict: Diccionario confirmando el ID único de auditoría registrado.
    """
    id_final = id_auditoria or str(uuid.uuid4())
    s3_key_final = s3_key if s3_key is not None else (ruta_imagen or "")
    registro = {
        "id": id_final,
        "folio": str(folio),
        "proveedor": str(proveedor),
        "total_items": total_items,
        "items": items_procesados,
        "estado": estado,
        "ruta_imagen": ruta_imagen or s3_key_final,
        "s3_key": s3_key_final,
        **validar_finanzas({**(datos_financieros or {}), "proveedor": proveedor, "total_factura": total_factura}),
        "cargos": cargos or [],
        "fecha_procesamiento": datetime.now(timezone.utc).isoformat()
    }
    if bitacora:
        registro["bitacora"] = bitacora

    logger.info("Guardando auditoría en DynamoDB para factura folio '%s' (ID: %s)...", folio, id_final)
    resultado = guardar_auditoria_factura_dynamo(registro)

    if resultado.get("exito"):
        return {
            "exito": True,
            "id_auditoria": id_final,
            "mensaje": f"Auditoría de factura folio '{folio}' guardada exitosamente en DynamoDB."
        }
    return {
        "exito": False,
        "error": resultado.get("error"),
        "mensaje": f"Fallo al guardar auditoría en DynamoDB: {resultado.get('error')}"
    }

