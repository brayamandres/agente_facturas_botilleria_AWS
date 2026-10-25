from worker.src.services.dynamodb import (
    crear_tablas_si_no_existen,
    buscar_productos_vectorial_nativo,
    obtener_producto_por_sku,
    consultar_duplicidad_en_historial,
    actualizar_stock_y_costo_dynamo,
    registrar_nuevo_producto_dynamo,
    guardar_auditoria_factura_dynamo,
)
from worker.src.services.embeddings import get_embedding_model, generar_embedding
from worker.src.services.gemini import get_gemini_client, extraer_datos_factura
from worker.src.services.llm import get_reasoning_llm
from worker.src.services.seed import sembrar_catalogo_dynamodb

