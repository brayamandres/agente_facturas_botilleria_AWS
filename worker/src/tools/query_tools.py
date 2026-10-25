"""
Herramientas de Consulta (Query Tools) para el agente LangGraph.
Interactúan directamente con Google Gemini Vision y Amazon DynamoDB (búsqueda vectorial nativa).
"""
import logging
from typing import Any, Dict, List, Optional
from langchain_core.tools import tool

from worker.src.services.dynamodb import (
    buscar_productos_vectorial_nativo,
    obtener_producto_por_sku,
    consultar_duplicidad_en_historial
)
from worker.src.services.embeddings import generar_embedding
from worker.src.services.gemini import extraer_datos_factura

logger = logging.getLogger("worker.tools.query_tools")


@tool
def extraer_factura_desde_imagen(ruta_imagen: str) -> Dict[str, Any]:
    """
    Extrae de forma estructurada los datos de una factura de compra (proveedor, número de folio
    y lista de ítems detallados con glosa, formato, cantidad y precio unitario) a partir de una
    imagen (PNG o JPG) utilizando visión multimodal con Google Gemini.
    
    Args:
        ruta_imagen (str): Ruta local o archivo de la imagen de la factura a procesar.
        
    Returns:
        dict: Diccionario con los datos extraídos de la factura.
    """
    return extraer_datos_factura(ruta_imagen)


@tool
def consultar_duplicidad_factura(
    folio: Optional[str] = None,
    proveedor: Optional[str] = None
) -> Dict[str, Any]:
    """
    Verifica en el historial de DynamoDB si la factura ya fue procesada anteriormente
    para evitar doble contabilización en el inventario.

    Args:
        folio (str | None): Folio o número de la factura a verificar.
        proveedor (str | None): Nombre del proveedor de la factura a verificar.
        
    Returns:
        dict: Diccionario con 'es_duplicada', 'estado_verificacion' y un mensaje explicativo.
    """
    return consultar_duplicidad_en_historial(folio=folio, proveedor=proveedor)


@tool
def buscar_candidatos_vectoriales(
    glosa: Optional[str] = None,
    formato: Optional[str] = None,
    top_k: int = 3
) -> List[Dict[str, Any]]:
    """
    Realiza una búsqueda semántica de los productos más cercanos en Amazon DynamoDB
    usando el método nativo search_vectors() con distancia COSINE.
    
    Args:
        glosa (str | None): Glosa de la línea de factura a buscar.
        formato (str | None): Formato detectado de la línea de factura.
        top_k (int, optional): Número máximo de candidatos a retornar. Default es 3.
    
    Returns:
        list[dict]: Lista de los productos candidatos más similares según el índice vectorial de DynamoDB.
    """
    glosa_limpia = str(glosa).strip() if glosa and str(glosa).strip().lower() != "null" else ""
    formato_limpio = str(formato).strip() if formato and str(formato).strip().lower() != "null" else ""

    if not glosa_limpia:
        return []

    texto_busqueda = f"{glosa_limpia} {formato_limpio}".strip()
    logger.info("Generando embedding para glosa de factura: '%s'", texto_busqueda)
    vector_query = generar_embedding(texto_busqueda)

    # Invocación exclusiva de la Búsqueda Vectorial Nativa de DynamoDB
    candidatos = buscar_productos_vectorial_nativo(vector_query=vector_query, top_k=top_k)
    return candidatos


@tool
def consultar_producto_por_sku(sku: str) -> Dict[str, Any]:
    """
    Obtiene los datos completos de un producto específico en DynamoDB mediante su clave primaria SKU.
    
    Args:
        sku (str): SKU del producto a consultar.
    
    Returns:
        dict: Diccionario con la clave 'encontrado' (True/False) y los datos del producto.
    """
    return obtener_producto_por_sku(sku)

