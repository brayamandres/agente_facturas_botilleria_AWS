"""Actualiza encabezados históricos desde JSON revisado, sin ejecutar el grafo ni tocar stock.

Uso: python -m worker.src.services.completar_finanzas --factura-id ID --archivo encabezado.json [--aplicar]
"""
import argparse
import json
from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal

from shared.finanzas import validar_finanzas


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--factura-id", required=True)
    parser.add_argument("--archivo", type=Path, required=True)
    parser.add_argument("--aplicar", action="store_true", help="Escribe solo el encabezado en el historial; sin esta opción muestra una vista previa.")
    args = parser.parse_args()
    datos = json.loads(args.archivo.read_text(encoding="utf-8-sig"), parse_float=Decimal)
    encabezado = validar_finanzas(datos)
    if not str(datos.get("proveedor") or "").strip():
        parser.error("El archivo debe incluir el proveedor.")
    encabezado["proveedor"] = str(datos["proveedor"]).strip()
    print(json.dumps(encabezado, ensure_ascii=False, indent=2, default=str))
    if not args.aplicar:
        print("Vista previa: no se escribió ningún dato. Usa --aplicar para guardar.")
        return
    from worker.config import get_dynamodb_resource, DYNAMODB_TABLE_HISTORIAL
    tabla = get_dynamodb_resource().Table(DYNAMODB_TABLE_HISTORIAL)
    actual = tabla.get_item(Key={"id": args.factura_id}, ConsistentRead=True).get("Item")
    if not actual or actual.get("estado") not in {"COMPLETADO", "REQUIERE_REVISION_MANUAL", "COMPLETADO_CON_ERRORES"}:
        parser.error("Factura inexistente o todavía en procesamiento.")
    encabezado["finanzas_actualizadas_en"] = datetime.now(timezone.utc).isoformat()
    nombres = {f"#f{i}": k for i, k in enumerate(encabezado)}
    valores = {f":v{i}": v for i, v in enumerate(encabezado.values())}
    nombres.update({"#id": "id", "#estado": "estado"})
    valores[":estado_actual"] = actual["estado"]
    tabla.update_item(Key={"id": args.factura_id}, UpdateExpression="SET " + ", ".join(f"#f{i} = :v{i}" for i in range(len(encabezado))), ExpressionAttributeNames=nombres, ExpressionAttributeValues=valores, ConditionExpression="attribute_exists(#id) AND #estado = :estado_actual")
    print("Encabezado actualizado. No se publicó ningún evento ni se modificó inventario.")


if __name__ == "__main__":
    main()
