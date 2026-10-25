"""
Script de Inicialización y Siembra (Seed) del Catálogo en Amazon DynamoDB.
Carga los 40 productos iniciales de la botillería generando vectores de 384 dimensiones
para poblar el índice nativo de búsqueda vectorial (VectorIndexes).
"""
import json
import logging
from decimal import Decimal
from pathlib import Path

from worker.config import (
    DYNAMODB_TABLE_PRODUCTOS,
    get_dynamodb_resource
)
from worker.src.services.dynamodb import crear_tablas_si_no_existen
from worker.src.services.embeddings import generar_embedding

logger = logging.getLogger("worker.services.seed")
DATA_FILE = Path(__file__).resolve().parent / "seed_data.json"


def sembrar_catalogo_dynamodb(forzar_recalculo: bool = False):
    """
    Inserta o actualiza los 40 productos base en la tabla DynamoDB 'botilleria-productos'.
    Genera los embeddings para alimentar el VectorIndex nativo de DynamoDB.
    """
    if not DATA_FILE.exists():
        raise FileNotFoundError(f"No se encontró el archivo de catálogo base en: {DATA_FILE}")

    # Asegurar que las tablas existan con el índice vectorial nativo
    crear_tablas_si_no_existen()

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        productos = json.load(f)

    resource = get_dynamodb_resource()
    table = resource.Table(DYNAMODB_TABLE_PRODUCTOS)

    print(f"\n[Seed] Iniciando indexación de {len(productos)} productos en DynamoDB...")
    contador_insertados = 0
    contador_omitidos = 0

    for i, prod in enumerate(productos, 1):
        sku = prod["sku"]

        if not forzar_recalculo:
            res = table.get_item(Key={"sku": sku})
            if "Item" in res and "embedding" in res["Item"]:
                contador_omitidos += 1
                continue

        # Generar texto descriptivo representativo y calcular embedding 384d
        texto = f"{prod.get('nombre', '')} {prod.get('marca', '')} {prod.get('categoria', '')} {prod.get('subcategoria', '')} {prod.get('formato', '')}".strip()
        vector = generar_embedding(texto)

        item = dict(prod)
        item["texto_descriptivo"] = texto
        item["precio_costo"] = Decimal(str(prod.get("precio_costo", 0)))
        item["precio_venta"] = Decimal(str(prod.get("precio_venta", 0)))
        item["stock_actual"] = int(prod.get("stock_actual", 0))
        item["stock_minimo"] = int(prod.get("stock_minimo", 0))
        item["embedding"] = [Decimal(str(round(v, 6))) for v in vector]

        table.put_item(Item=item)
        contador_insertados += 1
        print(f" [{i}/{len(productos)}] SKU indexado en VectorIndex: {sku} - {prod.get('nombre')}")

    print(f"\n[Seed] Proceso completado exitosamente en DynamoDB:")
    print(f" -> {contador_insertados} productos indexados con vector nativo.")
    print(f" -> {contador_omitidos} productos ya estaban presentes en el catálogo.\n")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sembrar_catalogo_dynamodb()

