"""
Microservicio Worker: Procesamiento Asíncrono de Facturas con LangGraph.

Patrón de Diseño:
- Arquitectura Orientada a Eventos (Event-Driven): SQS Long Polling (WaitTimeSeconds=20).
- Tolerancia a Fallos: ACK diferido (delete_message solo tras éxito total del grafo).
- Diseño Stateless (Sin estado local): Descarga de S3 a /tmp/ y limpieza inmediata.
- Observabilidad CloudWatch: Tiempos de ejecución, IDs de factura y captura estructurada de errores.
"""
import os
import sys
import json
import time
import logging
import argparse
from pathlib import Path
from typing import Dict, Any

from worker.config import (
    SQS_QUEUE_URL,
    S3_BUCKET_NAME,
    get_sqs_client,
    get_s3_client
)
from worker.src.agent.graph import crear_grafo_agente
from worker.src.services.dynamodb import crear_tablas_si_no_existen

# Configuración de Logging Estructurado para CloudWatch
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [WORKER] %(name)s - %(message)s"
)
logger = logging.getLogger("worker.main")


def procesar_factura_evento(evento: Dict[str, Any], grafo) -> bool:
    """
    Descarga la imagen desde S3 a /tmp/, ejecuta el pipeline agéntico con LangGraph
    y elimina el archivo temporal garantizando la condición Stateless.
    
    Args:
        evento (dict): Payload del mensaje SQS ({factura_id, s3_bucket, s3_key, ...}).
        grafo (CompiledGraph): Grafo de LangGraph compilado.
        
    Returns:
        bool: True si el procesamiento finalizó exitosamente; False si falló.
    """
    factura_id = evento.get("factura_id", "DESCONOCIDO")
    s3_bucket = evento.get("s3_bucket", S3_BUCKET_NAME)
    s3_key = evento.get("s3_key")

    logger.info("================================================================================")
    logger.info("Iniciando procesamiento de Factura ID: %s | S3: s3://%s/%s", factura_id, s3_bucket, s3_key)

    if not s3_key:
        logger.error("Evento descartado: 's3_key' ausente en el mensaje SQS: %s", evento)
        return False

    tiempo_inicio = time.time()
    ruta_temporal = Path(f"/tmp/{factura_id}_{Path(s3_key).name}")
    s3 = get_s3_client()

    try:
        # 1. Descarga temporal de la imagen desde S3
        logger.info("Descargando imagen desde S3 a '%s'...", ruta_temporal)
        ruta_temporal.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(s3_bucket, s3_key, str(ruta_temporal))

        # 2. Configurar Estado Inicial del Grafo LangGraph
        estado_inicial = {
            "ruta_imagen": str(ruta_temporal),
            "factura_id": factura_id,
            "s3_bucket": s3_bucket,
            "s3_key": s3_key,
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

        # 3. Ejecución del Grafo de LangGraph (con límite de recursión amplio para facturas de múltiples ítems)
        config = {
            "configurable": {"thread_id": f"worker_{factura_id}"},
            "recursion_limit": 300
        }
        logger.info("Ejecutando grafo LangGraph para Factura ID: %s (recursion_limit=300)...", factura_id)
        estado_final = grafo.invoke(estado_inicial, config)

        tiempo_fin = time.time()
        tiempo_total = tiempo_fin - tiempo_inicio

        estado_resultado = estado_final.get("estado_final", "DESCONOCIDO")
        total_items = len(estado_final.get("items_procesados", []))
        auditoria_id = estado_final.get("auditoria_id")

        if estado_resultado == "ERROR":
            logger.error(
                "Factura ID: %s finalizó con estado ERROR tras %.2f segundos. Ítems procesados: %d. Mensaje retenido en SQS.",
                factura_id, tiempo_total, total_items
            )
            logger.info("================================================================================")
            return False

        logger.info(
            "Factura ID: %s completada con éxito. Estado: %s | Ítems procesados: %d | "
            "Auditoría ID: %s | Tiempo total de procesamiento: %.2f segundos",
            factura_id, estado_resultado, total_items, auditoria_id, tiempo_total
        )
        logger.info("================================================================================")
        return True

    except Exception as e:
        tiempo_fin = time.time()
        tiempo_total = tiempo_fin - tiempo_inicio
        logger.error(
            "Fallo crítico durante el procesamiento de la Factura ID: %s tras %.2f segundos: %s",
            factura_id, tiempo_total, str(e), exc_info=True
        )
        return False

    finally:
        # 4. Diseño Stateless: Limpieza obligatoria de archivos en /tmp/
        if ruta_temporal.exists():
            try:
                ruta_temporal.unlink()
                logger.info("Archivo temporal '%s' eliminado. Contenedor en estado Stateless.", ruta_temporal)
            except Exception as ex_del:
                logger.warning("No fue posible eliminar el archivo temporal '%s': %s", ruta_temporal, ex_del)


def procesar_resolucion_humana(evento: Dict[str, Any]) -> bool:
    """
    Aplica transaccionalmente la decisión humana (HITL) tomada desde la interfaz web.
    Da de alta un nuevo producto con su vector nativo en DynamoDB o actualiza el stock existente.
    """
    factura_id = evento.get("factura_id")
    resolucion = evento.get("resolucion", {})
    accion = resolucion.get("accion")  # "CREAR_NUEVO", "ASOCIAR_EXISTENTE", "OMITIR"
    sku = resolucion.get("sku")
    glosa = resolucion.get("glosa_original")
    cantidad = int(resolucion.get("cantidad", 1))
    costo = float(resolucion.get("precio_costo", 0.0))
    venta = float(resolucion.get("precio_venta", 0.0)) if resolucion.get("precio_venta") else None

    logger.info("Procesando evento RESOLUCION_HUMANA para Factura ID %s: Acción=%s, SKU=%s", factura_id, accion, sku)

    from worker.src.tools.write_tools import registrar_nuevo_producto, actualizar_stock_y_costo
    from worker.config import get_dynamodb_resource, DYNAMODB_TABLE_HISTORIAL

    try:
        if accion == "CREAR_NUEVO" and sku:
            logger.info("Creando nuevo producto en DynamoDB con vector nativo para SKU: %s", sku)
            registrar_nuevo_producto.invoke({
                "sku": sku,
                "nombre": resolucion.get("nombre", glosa),
                "marca": resolucion.get("marca", "General"),
                "categoria": resolucion.get("categoria", "Destilados"),
                "subcategoria": resolucion.get("subcategoria", "General"),
                "formato": resolucion.get("formato", "Unidad"),
                "precio_costo": costo,
                "precio_venta": venta or (costo * 1.4),
                "stock_inicial": cantidad,
            })

        elif accion == "ASOCIAR_EXISTENTE" and sku:
            logger.info("Actualizando stock en DynamoDB para SKU: %s (+%d unidades)", sku, cantidad)
            actualizar_stock_y_costo.invoke({
                "sku": sku,
                "cantidad": cantidad,
                "nuevo_costo": costo,
                "nuevo_precio_venta": venta,
            })

        # Actualizar registro histórico en DynamoDB
        resource = get_dynamodb_resource()
        table = resource.Table(DYNAMODB_TABLE_HISTORIAL)
        res_factura = table.get_item(Key={"id": factura_id})
        item_factura = res_factura.get("Item", {})

        items = item_factura.get("items", [])
        for it in items:
            if it.get("glosa_original") == glosa or it.get("estado") == "REQUIERE_REVISION_MANUAL":
                it["estado"] = "RESUELTO_MANUAL" if accion != "OMITIR" else "OMITIDO_POR_HUMANO"
                it["sku_homologado"] = sku if accion != "OMITIR" else None

        hora_str = datetime.now().strftime("%H:%M:%S")
        log_msg = f"[{hora_str}] [RESOLUCIÓN APLICADA] Ítem '{glosa}' resuelto por humano: {accion} (SKU: {sku or 'OMITIDO'}). Inventario actualizado en DynamoDB."

        table.update_item(
            Key={"id": factura_id},
            UpdateExpression="SET #st = :nuevo_st, #items = :items, bitacora = list_append(if_not_exists(bitacora, :vacia), :nuevo_log)",
            ExpressionAttributeNames={
                "#st": "estado",
                "#items": "items"
            },
            ExpressionAttributeValues={
                ":nuevo_st": "COMPLETADO",
                ":items": items,
                ":nuevo_log": [log_msg],
                ":vacia": []
            }
        )
        logger.info("Factura ID %s actualizada a COMPLETADO tras resolución humana exitosa.", factura_id)
        return True

    except Exception as e:
        logger.error("Error al procesar resolución humana para factura ID %s: %s", factura_id, str(e), exc_info=True)
        return False


def iniciar_consumidor_sqs():
    """
    Bucle principal del Worker basado en Event-Driven y Long Polling de SQS.
    Escucha la cola, despacha eventos hacia LangGraph y confirma los mensajes (ACK)
    únicamente tras procesar la factura con éxito.
    """
    logger.info("Iniciando Microservicio Worker (LangGraph + AWS SQS + DynamoDB)...")

    # Asegurar tablas e índices vectoriales en DynamoDB al arranque
    try:
        crear_tablas_si_no_existen()
    except Exception as e:
        logger.warning("Aviso durante la verificación de tablas DynamoDB: %s", e)

    if not SQS_QUEUE_URL:
        logger.error("SQS_QUEUE_URL no está configurada en las variables de entorno. El worker no puede iniciar.")
        sys.exit(1)

    sqs = get_sqs_client()
    grafo = crear_grafo_agente()
    logger.info("Grafo LangGraph compilado. Escuchando cola SQS: %s (Long Polling 20s)...", SQS_QUEUE_URL)

    while True:
        try:
            # Long Polling: espera activa de hasta 20 segundos
            respuesta = sqs.receive_message(
                QueueUrl=SQS_QUEUE_URL,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=20,
                VisibilityTimeout=120  # 2 minutos para completar OCR + LangGraph
            )

            mensajes = respuesta.get("Messages", [])
            if not mensajes:
                continue

            for msg in mensajes:
                receipt_handle = msg.get("ReceiptHandle")
                cuerpo_str = msg.get("Body", "{}")

                try:
                    evento = json.loads(cuerpo_str)
                except json.JSONDecodeError:
                    logger.error("Mensaje SQS con JSON corrupto: %s. Eliminando...", cuerpo_str)
                    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                    continue

                # Procesar según tipo de evento
                tipo_evento = evento.get("tipo_evento")
                if tipo_evento == "RESOLUCION_HUMANA":
                    exito = procesar_resolucion_humana(evento)
                else:
                    exito = procesar_factura_evento(evento, grafo)

                # Tolerancia a Fallos: Solo eliminar el mensaje de SQS si el flujo fue exitoso
                if exito:
                    logger.info("Confirmando mensaje SQS (ACK delete_message) para Factura ID: %s", evento.get("factura_id"))
                    sqs.delete_message(QueueUrl=SQS_QUEUE_URL, ReceiptHandle=receipt_handle)
                else:
                    logger.warning(
                        "Factura ID: %s no completada. Mensaje retenido en SQS para reintento automático por expiración de visibilidad.",
                        evento.get("factura_id")
                    )

        except KeyboardInterrupt:
            logger.info("Detención manual del Worker solicitada. Cerrando proceso...")
            break
        except Exception as e:
            logger.error("Error imprevisto en el bucle principal de SQS: %s", str(e), exc_info=True)
            time.sleep(5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Worker Asíncrono de Procesamiento de Facturas (LangGraph)")
    parser.add_argument("--local-file", type=str, help="Procesa una imagen de factura local directamente para depuración")
    args = parser.parse_args()

    if args.local_file:
        logger.info("Modo de depuración local activado para archivo: %s", args.local_file)
        grafo = crear_grafo_agente()
        evento_local = {
            "factura_id": "test-local-debug",
            "s3_bucket": "local",
            "s3_key": args.local_file
        }
        # En modo local, simular lectura directa
        tiempo_inicio = time.time()
        estado = {
            "ruta_imagen": args.local_file,
            "factura_id": "test-local-debug",
            "s3_bucket": "local",
            "s3_key": args.local_file,
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
        res = grafo.invoke(estado, {"configurable": {"thread_id": "debug_thread"}})
        logger.info("Procesamiento local finalizado en %.2fs. Auditoría: %s", time.time() - tiempo_inicio, res.get("auditoria_id"))
    else:
        iniciar_consumidor_sqs()

