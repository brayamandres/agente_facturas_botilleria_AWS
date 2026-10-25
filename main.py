"""
Punto de Entrada Principal (CLI) - Agente de Facturas para Botillería (AWS).
Permite interactuar localmente con los componentes del Worker (LangGraph, DynamoDB nativo, Gemini)
y verificar el estado antes o después del despliegue en contenedores Docker.
"""
import os
import sys
import uuid
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from worker.config import (
    DATA_DIR,
    DYNAMODB_TABLE_PRODUCTOS,
    DYNAMODB_TABLE_HISTORIAL,
    get_dynamodb_resource
)
from worker.src.agent.graph import crear_grafo_agente
from worker.src.services.dynamodb import crear_tablas_si_no_existen
from worker.src.services.seed import sembrar_catalogo_dynamodb


def imprimir_banner():
    """Muestra el banner de presentación del sistema AWS."""
    print("=" * 80)
    print("   SISTEMA DISTRIBUIDO DE HOMOLOGACIÓN DE FACTURAS E INVENTARIO EN AWS")
    print("          Microservicios (FastAPI + Worker LangGraph + DynamoDB)")
    print("=" * 80)


def listar_facturas_disponibles():
    """Lista las imágenes de facturas existentes en data/."""
    if not DATA_DIR.exists():
        return []
    extensiones = (".png", ".jpg", ".jpeg")
    return [f for f in os.listdir(DATA_DIR) if f.lower().endswith(extensiones) and os.path.isfile(DATA_DIR / f)]


def ejecutar_agente_factura(ruta_imagen):
    """Ejecuta el grafo de LangGraph localmente usando el StateGraph del Worker."""
    print("\n" + "#" * 80)
    print(f" INICIANDO PIPELINE AGÉNTICO (LangGraph + Búsqueda Vectorial DynamoDB)")
    print(f" Archivo a procesar: {ruta_imagen.name}")
    print("#" * 80)

    grafo = crear_grafo_agente()
    factura_id = f"cli-{uuid.uuid4().hex[:8]}"
    thread_id = f"thread_{factura_id}"
    config = {"configurable": {"thread_id": thread_id}}

    estado_inicial = {
        "ruta_imagen": str(ruta_imagen),
        "factura_id": factura_id,
        "s3_bucket": "local-cli",
        "s3_key": ruta_imagen.name,
        "datos_factura": {},
        "es_duplicada": False,
        "info_duplicada": None,
        "items_pendientes": [],
        "item_actual": None,
        "candidatos_actuales": [],
        "decision_actual": None,
        "alerta_precio": None,
        "items_procesados": [],
        "requiere_intervencion": False,
        "datos_intervencion_humana": None,
        "auditoria_id": None,
        "mensajes_bitacora": [],
        "estado_final": "INICIADO"
    }

    t0 = time.time()
    estado_final = grafo.invoke(estado_inicial, config)
    t_total = time.time() - t0

    print("\n" + "=" * 80)
    print(f" RESULTADO FINAL DEL PROCESO ({t_total:.2f}s)")
    print("=" * 80)
    print(f" Estado final: {estado_final.get('estado_final')}")
    print(f" ID de Auditoría DynamoDB: {estado_final.get('auditoria_id')}")
    print(f" Total ítems procesados: {len(estado_final.get('items_procesados', []))}")
    for item in estado_final.get("items_procesados", []):
        print(f"  • {item.get('glosa_original')} -> SKU: {item.get('sku_homologado')} | Cantidad: {item.get('cantidad')} | Estado: {item.get('estado')}")
    print("=" * 80 + "\n")


def ver_catalogo_dynamodb():
    """Consulta y lista los productos registrados en DynamoDB."""
    print(f"\nConsultando tabla '{DYNAMODB_TABLE_PRODUCTOS}' en DynamoDB...")
    try:
        resource = get_dynamodb_resource()
        table = resource.Table(DYNAMODB_TABLE_PRODUCTOS)
        res = table.scan(Limit=50)
        items = res.get("Items", [])
        if not items:
            print("No hay productos registrados en la tabla de DynamoDB.")
            return

        print(f"\n{'SKU':<18} | {'NOMBRE':<35} | {'STOCK':<6} | {'COSTO':<8} | {'VENTA':<8}")
        print("-" * 85)
        for prod in items:
            print(f"{str(prod.get('sku')):<18} | {str(prod.get('nombre', ''))[:35]:<35} | {str(prod.get('stock_actual', 0)):<6} | ${str(prod.get('precio_costo', 0)):<7} | ${str(prod.get('precio_venta', 0)):<7}")
        print(f"\nTotal visualizados: {len(items)} productos.\n")
    except Exception as e:
        print(f"❌ Error al consultar DynamoDB: {e}")


def ver_historial_auditoria_dynamodb():
    """Consulta la tabla de auditoría en DynamoDB."""
    print(f"\nConsultando historial de facturas en tabla '{DYNAMODB_TABLE_HISTORIAL}'...")
    try:
        resource = get_dynamodb_resource()
        table = resource.Table(DYNAMODB_TABLE_HISTORIAL)
        res = table.scan(Limit=20)
        items = res.get("Items", [])
        if not items:
            print("No hay facturas registradas en la auditoría de DynamoDB.")
            return

        print(f"\n{'FOLIO':<15} | {'PROVEEDOR':<25} | {'ESTADO':<15} | {'ÍTEMS':<6} | {'FECHA'}")
        print("-" * 90)
        for h in items:
            print(f"{str(h.get('folio')):<15} | {str(h.get('proveedor', ''))[:25]:<25} | {str(h.get('estado')):<15} | {str(h.get('total_items', 0)):<6} | {str(h.get('fecha_procesamiento', ''))[:19]}")
        print(f"\nTotal registradas: {len(items)} facturas.\n")
    except Exception as e:
        print(f"❌ Error al consultar historial en DynamoDB: {e}")


def mostrar_guia_docker():
    """Muestra la guía de comandos para levantar contenedores y medir con docker stats."""
    print("\n" + "=" * 80)
    print(" GUÍA DE DESPLIEGUE DOCKER Y MEDICIÓN CON 'docker stats'")
    print("=" * 80)
    print("1. Iniciar ambos microservicios en segundo plano:")
    print("   docker compose up --build -d\n")
    print("2. Monitorear consumo exacto de CPU y Memoria RAM en tiempo real:")
    print("   docker stats\n")
    print("   Consumos esperados de referencia:")
    print("   - API (FastAPI):     ~100 MB a 120 MB RAM")
    print("   - Worker (LangGraph): ~300 MB a 380 MB RAM\n")
    print("3. Verificar sondas de salud (Health Check):")
    print("   curl http://localhost:8000/health\n")
    print("4. Enviar factura de prueba a través de la API (HTTP 202 Accepted):")
    print("   curl -X POST http://localhost:8000/facturas/upload -F 'file=@data/factura1.png'\n")
    print("5. Ver logs del Worker procesando el mensaje SQS:")
    print("   docker logs -f botilleria_worker\n")
    print("=" * 80 + "\n")


def menu_principal():
    """Menú interactivo CLI."""
    crear_tablas_si_no_existen()
    ultima_factura = None

    while True:
        imprimir_banner()
        print(" [1] Procesar una factura con LangGraph (Modo Local Directo)")
        print(" [2] Probar control de duplicados en DynamoDB")
        print(" [3] Visualizar inventario en Amazon DynamoDB")
        print(" [4] Consultar historial de auditoría en Amazon DynamoDB")
        print(" [5] Sembrar / Indexar catálogo con VectorIndexes en DynamoDB")
        print(" [6] Ver guía de despliegue Docker y medición con 'docker stats'")
        print(" [7] Salir")
        print("=" * 80)

        opcion = input("Selecciona una opción (1-7): ").strip()

        if opcion == "1":
            facturas = listar_facturas_disponibles()
            if not facturas:
                print(f"\n⚠️ No se encontraron facturas en '{DATA_DIR}'.")
            else:
                for idx, f in enumerate(facturas, 1):
                    print(f"  [{idx}] {f}")
                sel = input(f"\nSelecciona una factura (1-{len(facturas)}) [Enter='{facturas[0]}']: ").strip()
                ruta = DATA_DIR / (facturas[int(sel) - 1] if sel.isdigit() and 1 <= int(sel) <= len(facturas) else facturas[0])
                ultima_factura = ruta
                ejecutar_agente_factura(ruta)
            input("\nPresiona Enter para continuar...")

        elif opcion == "2":
            facturas = listar_facturas_disponibles()
            if not facturas:
                print("\n⚠️ No hay facturas en data/.")
            else:
                ruta = ultima_factura or (DATA_DIR / facturas[0])
                print(f"\nRe-procesando '{ruta.name}' para verificar bloqueo de duplicados...")
                ejecutar_agente_factura(ruta)
            input("\nPresiona Enter para continuar...")

        elif opcion == "3":
            ver_catalogo_dynamodb()
            input("Presiona Enter para continuar...")

        elif opcion == "4":
            ver_historial_auditoria_dynamodb()
            input("Presiona Enter para continuar...")

        elif opcion == "5":
            sembrar_catalogo_dynamodb()
            input("Presiona Enter para continuar...")

        elif opcion == "6":
            mostrar_guia_docker()
            input("Presiona Enter para continuar...")

        elif opcion == "7":
            print("\n¡Sesión finalizada exitosamente! Hasta luego.\n")
            break


if __name__ == "__main__":
    menu_principal()
