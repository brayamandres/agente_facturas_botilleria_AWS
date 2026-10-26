"""
Orquestador de Flujo del Agente de Facturas con LangGraph para el Worker.

Implementa una Máquina de Estados Finita (StateGraph) adaptada a AWS:
- Nodos especializados que ejecutan las 9 herramientas conectadas a DynamoDB y Gemini.
- Bordes condicionales para toma de decisiones adaptativas (duplicados, anomalías de precios, baja confianza).
- Ciclos de iteración (loops) para procesar múltiples ítems secuencialmente.
- Soporte dual interactivo (CLI local) y headless (Docker / AWS ECS sin bloqueo de stdin).
- Memoria de corto plazo (MemorySaver checkpointer).
"""
import sys
import logging
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List
from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from shared.finanzas import validar_finanzas, CAMPOS_FINANCIEROS, monto_decimal

from worker.src.agent.state import AgentState
from worker.src.tools.reasoning_tools import validar_respuesta_homologacion
from worker.src.tools import (
    extraer_factura_desde_imagen,
    consultar_duplicidad_factura,
    buscar_candidatos_vectoriales,
    consultar_producto_por_sku,
    razonar_homologacion,
    evaluar_variacion_costo,
    actualizar_stock_y_costo,
    registrar_nuevo_producto,
    registrar_auditoria_factura,
)

logger = logging.getLogger("worker.agent.graph")


def _registrar_progreso_en_dynamodb(
    factura_id: Optional[str],
    mensaje: str,
    estado_parcial: Optional[str] = None,
    proveedor: Optional[str] = None,
    folio: Optional[str] = None,
    items: Optional[List[Dict[str, Any]]] = None
):
    """Actualiza en tiempo real la bitácora episódica y estado en DynamoDB para seguimiento web."""
    if not factura_id or str(factura_id) in ["test-local-debug", "DESCONOCIDO", "local"]:
        return
    try:
        from worker.config import get_dynamodb_resource, DYNAMODB_TABLE_HISTORIAL
        resource = get_dynamodb_resource()
        table = resource.Table(DYNAMODB_TABLE_HISTORIAL)
        now_str = datetime.now().strftime("%H:%M:%S")
        linea = f"[{now_str}] {mensaje}"

        update_expr = "SET bitacora = list_append(if_not_exists(bitacora, :vacia), :nuevo_log)"
        expr_values: Dict[str, Any] = {
            ":nuevo_log": [linea],
            ":vacia": []
        }
        expr_names: Dict[str, str] = {}
        if estado_parcial:
            update_expr += ", #st = :st"
            expr_names["#st"] = "estado"
            expr_values[":st"] = estado_parcial
        if proveedor and str(proveedor).strip():
            update_expr += ", proveedor = :prov"
            expr_values[":prov"] = str(proveedor).strip()
        if folio and str(folio).strip():
            update_expr += ", folio = :fol"
            expr_values[":fol"] = str(folio).strip()
        if items is not None:
            from decimal import Decimal
            def _sanitizar(v):
                if isinstance(v, float):
                    return Decimal(str(v))
                if isinstance(v, dict):
                    return {k: _sanitizar(val) for k, val in v.items()}
                if isinstance(v, list):
                    return [_sanitizar(elem) for elem in v]
                return v
            update_expr += ", #items = :items"
            expr_names["#items"] = "items"
            expr_values[":items"] = _sanitizar(items)

        kwargs: Dict[str, Any] = {
            "Key": {"id": factura_id},
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": expr_values
        }
        if expr_names:
            kwargs["ExpressionAttributeNames"] = expr_names

        table.update_item(**kwargs)
    except Exception as e:
        logger.debug("No se pudo registrar progreso en DynamoDB para %s: %s", factura_id, e)


# ============================================================================
# FUNCIONES AUXILIARES DE VALIDACIÓN Y PARSEO
# ============================================================================

def _limpiar_texto(valor: Any) -> str:
    """Normaliza un campo de texto devolviendo cadena vacía si es None o 'null'."""
    if valor is None:
        return ""
    texto = str(valor).strip()
    if texto.lower() in ["null", "none", "n/a", "ilegible"]:
        return ""
    return texto


def _parsear_entero_positivo(valor: Any) -> Optional[int]:
    """Convierte un valor a entero positivo; retorna None si es ilegible o <= 0."""
    if valor is None:
        return None
    try:
        num = int(round(float(valor)))
        return num if num > 0 else None
    except (ValueError, TypeError):
        return None


def _parsear_float_positivo(valor: Any) -> Optional[float]:
    """Convierte un valor a float positivo; retorna None si es ilegible o <= 0."""
    if valor is None:
        return None
    try:
        num = float(valor)
        return num if num > 0 else None
    except (ValueError, TypeError):
        return None


def _obtener_campos_ilegibles(item: Dict[str, Any]) -> List[str]:
    """Identifica qué campos clave de una línea de factura son ilegibles o ausentes."""
    campos = []
    if not _limpiar_texto(item.get("nombre_glosa")):
        campos.append("nombre/glosa")
    if _parsear_entero_positivo(item.get("cantidad")) is None:
        campos.append("cantidad")
    if _parsear_float_positivo(item.get("precio_unitario")) is None:
        campos.append("precio unitario")
    return campos


# ============================================================================
# 1. NODOS DEL GRAFO (Execution Nodes)
# ============================================================================

def node_extraer(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 1: Invoca la herramienta multimodal para extraer encabezado e ítems desde la imagen.
    """
    ruta_imagen = state["ruta_imagen"]
    logger.info("[Nodo Extraer] Extrayendo datos de la factura: '%s'...", ruta_imagen)

    datos_factura = extraer_factura_desde_imagen.invoke({"ruta_imagen": ruta_imagen}) or {}
    datos_factura.update(validar_finanzas(datos_factura))
    items = [i for i in (datos_factura.get("items") or []) if str(i.get("tipo_item", "PRODUCTO")).upper() != "CARGO"]

    msg = (
        f"Factura extraída con éxito. Proveedor: '{datos_factura.get('proveedor')}', "
        f"Folio: '{datos_factura.get('numero_factura')}', Ítems detectados: {len(items)}"
    )
    logger.info(" -> %s", msg)
    _registrar_progreso_en_dynamodb(
        state.get("factura_id"),
        msg,
        estado_parcial="ANALIZANDO",
        proveedor=datos_factura.get("proveedor"),
        folio=datos_factura.get("numero_factura")
    )

    return {
        "datos_factura": datos_factura,
        "items_pendientes": items,
        "items_procesados": [],
        "mensajes_bitacora": state.get("mensajes_bitacora", []) + [msg],
        "estado_final": "EN_PROCESO"
    }


def node_verificar_duplicidad(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 2: Verifica en la memoria de largo plazo (DynamoDB) si la factura ya fue procesada.
    """
    datos = state["datos_factura"]
    folio = datos.get("numero_factura")
    proveedor = datos.get("proveedor")
    factura_id = state.get("factura_id")

    logger.info("[Nodo Duplicidad] Comprobando en DynamoDB para folio '%s' de '%s' (excluyendo ID: %s)...", folio, proveedor, factura_id)
    resultado = consultar_duplicidad_factura.invoke({
        "folio": folio,
        "proveedor": proveedor,
        "factura_id_actual": factura_id
    })

    es_duplicada = bool(resultado.get("es_duplicada", False))
    estado_verificacion = resultado.get("estado_verificacion", "")
    msg = resultado.get("mensaje", "")
    logger.info(" -> %s", msg)

    actualizacion = {
        "es_duplicada": es_duplicada,
        "info_duplicada": resultado.get("registro_existente"),
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }
    if estado_verificacion == "ERROR_CONSULTA" or resultado.get("es_duplicada") is None:
        actualizacion["estado_final"] = "ERROR"

    return actualizacion


def node_manejar_duplicada(state: AgentState) -> Dict[str, Any]:
    """
    Nodo Alternativo: Detiene el flujo adaptativamente si la factura ya existe en DynamoDB
    o si ocurrió un error al consultar la base de datos.
    """
    factura_id = state.get("factura_id")

    if state.get("estado_final") == "ERROR":
        msg = "ERROR: Proceso abortado debido a un fallo en la consulta a DynamoDB."
        logger.error("[Nodo Control] %s", msg)
        _registrar_progreso_en_dynamodb(
            factura_id,
            msg,
            estado_parcial="ERROR"
        )
        return {
            "estado_final": "ERROR",
            "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
        }

    info = state.get("info_duplicada") or {}
    folio = state["datos_factura"].get("numero_factura", "S/F")
    proveedor = state["datos_factura"].get("proveedor", "Proveedor")
    fecha = info.get("fecha", "fecha previa")
    msg = (
        f"⚠️ FACTURA DUPLICADA: Proceso abortado para evitar duplicar stock. "
        f"La factura folio '{folio}' de '{proveedor}' ya fue registrada previamente el {fecha}."
    )
    logger.warning("[Nodo Control] %s", msg)
    _registrar_progreso_en_dynamodb(
        factura_id,
        msg,
        estado_parcial="DUPLICADO_IGNORADO",
        proveedor=proveedor,
        folio=folio
    )

    return {
        "estado_final": "DUPLICADO_IGNORADO",
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }


def node_preparar_siguiente_item(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 3 (Loop): Toma el primer ítem de la lista de pendientes para su evaluación individual.
    """
    pendientes = state["items_pendientes"]
    item_actual = pendientes[0]
    restantes = pendientes[1:]

    unidades_pack = _parsear_entero_positivo(item_actual.get("unidades")) or 1
    msg = (
        f"Iniciando evaluación de ítem: '{item_actual.get('nombre_glosa')}' "
        f"(Formato: {item_actual.get('formato')}, Cantidad: {item_actual.get('cantidad')}, Unidades/Pack: {unidades_pack})"
    )
    logger.info("[Nodo Loop Ítem] %s", msg)

    return {
        "item_actual": item_actual,
        "items_pendientes": restantes,
        "candidatos_actuales": [],
        "decision_actual": None,
        "alerta_precio": None,
        "requiere_intervencion": False,
        "datos_intervencion_humana": None,
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }


def node_recuperar_candidatos(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 4: Búsqueda vectorial nativa de los top 3 candidatos en Amazon DynamoDB.
    """
    item = state["item_actual"] or {}
    glosa = _limpiar_texto(item.get("nombre_glosa"))
    formato = _limpiar_texto(item.get("formato"))

    if not glosa:
        msg = "Nombre/glosa ilegible en la factura. Se omite búsqueda vectorial y se enviará a revisión humana."
        logger.warning("[Nodo Retrieval] %s", msg)
        return {
            "candidatos_actuales": [],
            "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
        }

    logger.info("[Nodo Retrieval] Buscando candidatos vectoriales en DynamoDB para '%s'...", glosa)
    candidatos = buscar_candidatos_vectoriales.invoke({"glosa": glosa, "formato": formato, "top_k": 3})

    nombres_candidatos = [f"{c['sku']} ({c['nombre']})" for c in candidatos]
    msg = f"Candidatos recuperados de DynamoDB: {', '.join(nombres_candidatos) if nombres_candidatos else 'Ninguno'}"
    logger.info(" -> %s", msg)
    _registrar_progreso_en_dynamodb(state.get("factura_id"), f"DynamoDB VectorIndexes: {msg}")

    return {
        "candidatos_actuales": candidatos,
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }


def node_razonar_homologacion(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 5: El LLM razona sobre abreviaciones y homologa contra los candidatos de DynamoDB.
    """
    item = state["item_actual"] or {}
    candidatos = state["candidatos_actuales"]
    glosa = _limpiar_texto(item.get("nombre_glosa"))
    formato = _limpiar_texto(item.get("formato"))

    logger.info("[Nodo Razonamiento LLM] Evaluando homologación para '%s'...", glosa)

    if not glosa:
        decision = {
            "sku_seleccionado": None,
            "confianza": 0.0,
            "justificacion": "El nombre o glosa del producto no es legible en la factura.",
            "requiere_revision": True
        }
    else:
        decision_cruda = razonar_homologacion.invoke({
            "glosa_factura": glosa,
            "formato_factura": formato,
            "candidatos": candidatos
        }) or {}
        decision = validar_respuesta_homologacion(decision_cruda, candidatos=candidatos)

    sku = decision.get("sku_seleccionado")
    confianza = float(decision.get("confianza") or 0.0)
    justificacion = decision.get("justificacion") or ""

    msg = f"Decisión de homologación: SKU={sku} (Confianza={confianza:.2f}) | Motivo: {justificacion}"
    logger.info(" -> %s", msg)
    _registrar_progreso_en_dynamodb(state.get("factura_id"), f"Razonamiento IA: {msg}")

    return {
        "decision_actual": decision,
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }


def node_evaluar_costo(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 6: Evalúa si existe variación abrupta de costo en comparación al histórico de DynamoDB.
    """
    decision = state["decision_actual"] or {}
    item = state["item_actual"] or {}
    sku = decision.get("sku_seleccionado")
    costo_factura = _parsear_float_positivo(item.get("precio_unitario"))

    alerta_precio = None
    if sku and costo_factura is not None:
        prod_info = consultar_producto_por_sku.invoke({"sku": sku})
        if prod_info.get("encontrado"):
            costo_actual = float(prod_info["producto"].get("precio_costo") or 0)
            alerta_precio = evaluar_variacion_costo.invoke({
                "costo_factura": costo_factura,
                "costo_actual": costo_actual,
                "umbral_alerta": 0.15
            })

            if alerta_precio.get("alerta"):
                logger.warning("[Nodo Costo] %s", alerta_precio.get("mensaje"))

    return {
        "alerta_precio": alerta_precio
    }


def node_intervencion_humana(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 7: Human-in-the-Loop.
    Maneja modo interactivo si hay TTY (CLI local) o modo Headless (Docker/Worker en ECS)
    para no bloquear el hilo de ejecución asíncrono.
    """
    item = state["item_actual"] or {}
    decision = state["decision_actual"] or {}
    alerta = state["alerta_precio"] or {}
    sku = decision.get("sku_seleccionado")
    confianza = float(decision.get("confianza") or 0.0)

    glosa = _limpiar_texto(item.get("nombre_glosa"))
    formato = _limpiar_texto(item.get("formato"))
    costo_factura = _parsear_float_positivo(item.get("precio_unitario"))
    cantidad = _parsear_entero_positivo(item.get("cantidad"))
    unidades_pack = _parsear_entero_positivo(item.get("unidades")) or 1
    campos_ilegibles = _obtener_campos_ilegibles(item)

    logger.info("[HITL] Ítem derivado a revisión humana. Glosa: '%s', SKU: '%s', Confianza: %.2f", glosa, sku, confianza)

    # Modo Headless / Asíncrono en Worker (Docker / AWS ECS sin terminal interactiva)
    if not sys.stdin.isatty():
        logger.warning(
            "[HITL Headless] Contenedor sin TTY interactiva. Registrando ítem para auditoría humana posterior."
        )
        _registrar_progreso_en_dynamodb(
            state.get("factura_id"),
            f"⚠️ Revisión Humana Requerida: '{glosa}' (Confianza: {confianza:.2f}). Esperando respuesta del usuario en la interfaz web.",
            estado_parcial="REQUIERE_REVISION_MANUAL",
            items=state.get("items_procesados", []) + [{
                "glosa_original": glosa,
                "formato": formato,
                "cantidad": (cantidad or 1) * unidades_pack,
                "unidades_por_pack": unidades_pack,
                "precio_unitario": costo_factura or 0.0,
                "sku_homologado": sku,
                "estado": "REQUIERE_REVISION_MANUAL"
            }]
        )
        datos_accion = {
            "tipo_accion": "REQUIERE_AUDITORIA_HUMANA",
            "sku": sku,
            "motivo": f"Confianza: {confianza:.2f}. Alerta costo: {alerta.get('alerta', False)}. Campos ilegibles: {campos_ilegibles}"
        }
        return {
            "datos_intervencion_humana": datos_accion,
            "requiere_intervencion": False
        }

    # Modo Interactivo (CLI local con usuario presente)
    print("\n" + "="*70)
    print(" [HUMAN-IN-THE-LOOP] Se requiere intervención humana para este ítem:")
    print(
        f" Glosa Factura: '{glosa or 'ILEGIBLE'}' | "
        f"Formato: {formato or 'No especificado'} | "
        f"Costo: {costo_factura} | "
        f"Cantidad: {cantidad} (Unidades x pack: {unidades_pack})"
    )

    if not sku or decision.get("requiere_revision") or confianza < 0.75:
        resp = input(" -> ¿Deseas agregar este producto como NUEVO en DynamoDB? (s/n): ").strip().lower()
        if resp in ["s", "si", "y", "yes"]:
            sku_sug = (glosa or "SKU-NUEVO").upper().replace(" ", "-")[:15]
            nuevo_sku = input(f"    * SKU único [Enter para '{sku_sug}']: ").strip().upper() or sku_sug
            nombre = input(f"    * Nombre comercial [Enter para '{glosa}']: ").strip() or glosa
            marca = input("    * Marca: ").strip() or "Genérico"
            categoria = input("    * Categoría: ").strip() or "Destilados"
            subcategoria = input("    * Subcategoría: ").strip() or "General"
            nuevo_formato = input(f"    * Formato [Enter para '{formato or 'Unidad'}']: ").strip() or (formato or "Unidad")
            costo = costo_factura or 1000.0
            pv = float(input(f"    * Precio de venta [Costo: {costo}]: ").strip() or (costo * 1.4))

            datos_accion = {
                "tipo_accion": "CREAR_NUEVO",
                "sku": nuevo_sku,
                "nombre": nombre,
                "marca": marca,
                "categoria": categoria,
                "subcategoria": subcategoria,
                "formato": nuevo_formato,
                "precio_costo": costo,
                "precio_venta": pv,
                "stock_inicial": (cantidad or 1) * unidades_pack,
                "unidades_incluidas": True
            }
        else:
            datos_accion = {"tipo_accion": "OMITIR", "sku": None}
    else:
        datos_accion = {
            "tipo_accion": "ACTUALIZAR_EXISTENTE",
            "sku": sku,
            "cantidad": (cantidad or 1) * unidades_pack,
            "precio_costo": costo_factura or 0.0,
            "nuevo_precio_venta": None,
            "unidades_incluidas": True
        }

    return {
        "datos_intervencion_humana": datos_accion,
        "requiere_intervencion": False
    }


def node_aplicar_cambios(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 8: Aplica las escrituras atómicas en Amazon DynamoDB (actualizar stock o crear nuevo producto).
    """
    item = state["item_actual"] or {}
    decision = state["decision_actual"] or {}
    accion = state.get("datos_intervencion_humana")
    folio_base = _limpiar_texto(state.get("datos_factura", {}).get("numero_factura")) or None
    indice_linea = len(state.get("items_procesados", [])) + 1
    folio = f"{folio_base}#L{indice_linea}" if folio_base else None

    cantidad_bultos = _parsear_entero_positivo(item.get("cantidad")) or 0
    unidades_pack = _parsear_entero_positivo(item.get("unidades")) or 1
    unidades_totales = cantidad_bultos * unidades_pack

    registro_item = {
        "linea": indice_linea,
        "glosa_original": _limpiar_texto(item.get("nombre_glosa")) or "ILEGIBLE",
        "formato": _limpiar_texto(item.get("formato")) or None,
        "cantidad": unidades_totales,
        "unidades_por_pack": unidades_pack,
        "precio_unitario": _parsear_float_positivo(item.get("precio_unitario")) or 0.0,
        "precio_facturado": monto_decimal(item.get("precio_unitario")),
        "cantidad_facturada": monto_decimal(item.get("cantidad")),
        "base_precio": str(item.get("base_precio")).upper() if str(item.get("base_precio")).upper() in {"UNIDAD", "BULTO"} else None,
        "monto_linea_neto": monto_decimal(item.get("monto_linea_neto")),
        "sku_homologado": None,
        "estado": "PENDIENTE"
    }
    msg = ""

    if accion:
        tipo = accion.get("tipo_accion")
        if tipo == "CREAR_NUEVO":
            resultado = registrar_nuevo_producto.invoke({
                "sku": accion["sku"],
                "nombre": accion["nombre"],
                "marca": accion["marca"],
                "categoria": accion["categoria"],
                "subcategoria": accion["subcategoria"],
                "formato": accion["formato"],
                "precio_costo": accion["precio_costo"],
                "precio_venta": accion["precio_venta"],
                "stock_inicial": accion["stock_inicial"],
                "folio_factura": folio,
            }) or {}

            msg = resultado.get("mensaje", "")
            registro_item["sku_homologado"] = accion["sku"]
            registro_item["estado"] = "CREADO_NUEVO" if resultado.get("exito") else "ERROR_ESCRITURA"

        elif tipo == "ACTUALIZAR_EXISTENTE":
            resultado = actualizar_stock_y_costo.invoke({
                "sku": accion["sku"],
                "cantidad": accion["cantidad"],
                "nuevo_costo": accion["precio_costo"],
                "nuevo_precio_venta": accion.get("nuevo_precio_venta"),
                "folio_factura": folio,
            }) or {}

            msg = resultado.get("mensaje", "")
            registro_item["sku_homologado"] = accion["sku"]
            registro_item["estado"] = "ACTUALIZADO" if resultado.get("exito") else "ERROR_ESCRITURA"

        elif tipo == "REQUIERE_AUDITORIA_HUMANA":
            registro_item["estado"] = "REQUIERE_REVISION_MANUAL"
            registro_item["sku_homologado"] = accion.get("sku")
            msg = f"Ítem '{registro_item['glosa_original']}' marcado para revisión manual en DynamoDB."

        else:
            registro_item["estado"] = "OMITIDO_POR_HUMANO"
            msg = "Ítem omitido por el operador."

    else:
        # Homologación automática
        sku = decision.get("sku_seleccionado")
        cantidad_base = _parsear_entero_positivo(item.get("cantidad")) or 1
        cantidad_total = cantidad_base * unidades_pack
        costo = _parsear_float_positivo(item.get("precio_unitario")) or 0.0

        resultado = actualizar_stock_y_costo.invoke({
            "sku": sku,
            "cantidad": cantidad_total,
            "nuevo_costo": costo,
            "folio_factura": folio,
        }) or {}

        msg = resultado.get("mensaje", "")
        registro_item["cantidad"] = cantidad_total
        registro_item["sku_homologado"] = sku
        registro_item["estado"] = "AUTO_HOMOLOGADO" if resultado.get("exito") else "ERROR_ESCRITURA"

    logger.info("[Nodo Escritura DynamoDB] %s | Estado: %s", msg, registro_item["estado"])
    items_actualizados = list(state.get("items_procesados", [])) + [registro_item]
    bitacora = list(state.get("mensajes_bitacora", [])) + ([msg] if msg else [])

    # Registrar progreso en tiempo real para visualización en terminal web
    _registrar_progreso_en_dynamodb(
        state.get("factura_id"),
        f"[{registro_item['estado']}] {msg}",
        items=items_actualizados
    )

    return {
        "items_procesados": items_actualizados,
        "mensajes_bitacora": bitacora
    }


def node_registrar_auditoria(state: AgentState) -> Dict[str, Any]:
    """
    Nodo 9: Guarda el comprobante de auditoría en la tabla 'botilleria-historial-facturas' de DynamoDB.
    """
    datos = state["datos_factura"]
    folio = _limpiar_texto(datos.get("numero_factura")) or "S/F"
    proveedor = _limpiar_texto(datos.get("proveedor")) or "DESCONOCIDO"
    items_procesados = state.get("items_procesados", [])

    hubo_errores = any(i.get("estado") == "ERROR_ESCRITURA" for i in items_procesados)
    hay_pendientes_revision = any(i.get("estado") == "REQUIERE_REVISION_MANUAL" for i in items_procesados)

    if hubo_errores:
        estado_auditoria = "COMPLETADO_CON_ERRORES"
    elif hay_pendientes_revision:
        estado_auditoria = "REQUIERE_REVISION_MANUAL"
    else:
        estado_auditoria = "COMPLETADO"

    logger.info("[Nodo Auditoría] Guardando registro en DynamoDB para folio '%s'...", folio)

    s3_key = state.get("s3_key")
    ruta_imagen = s3_key or state.get("ruta_imagen")

    resultado = registrar_auditoria_factura.invoke({
        "folio": folio,
        "proveedor": proveedor,
        "total_items": len(items_procesados),
        "items_procesados": items_procesados,
        "estado": estado_auditoria,
        "ruta_imagen": ruta_imagen,
        "s3_key": s3_key,
        "total_factura": datos.get("total_factura"),
        "datos_financieros": {k: datos.get(k) for k in CAMPOS_FINANCIEROS},
        "cargos": [i for i in datos.get("items", []) if str(i.get("tipo_item", "PRODUCTO")).upper() == "CARGO"],
        "id_auditoria": state.get("factura_id"),
        "bitacora": state.get("mensajes_bitacora", []) + [f"Factura folio '{folio}' procesada ({estado_auditoria})."]
    }) or {}

    if not resultado.get("exito"):
        err_msg = resultado.get("mensaje") or resultado.get("error") or "Fallo al registrar auditoría en DynamoDB."
        logger.error("[Nodo Auditoría] %s", err_msg)
        return {
            "auditoria_id": None,
            "estado_final": "ERROR",
            "mensajes_bitacora": state["mensajes_bitacora"] + [f"ERROR: {err_msg}"]
        }

    id_auditoria = resultado.get("id_auditoria")
    msg = f"Factura folio '{folio}' procesada ({estado_auditoria}). Auditoría ID: {id_auditoria}"
    logger.info(" -> %s", msg)

    return {
        "auditoria_id": id_auditoria,
        "estado_final": estado_auditoria,
        "mensajes_bitacora": state["mensajes_bitacora"] + [msg]
    }


# ============================================================================
# 2. BORDES CONDICIONALES
# ============================================================================

def decidir_ruta_duplicidad(state: AgentState) -> str:
    """Borde condicional 1: Bloquea si la factura ya existe o arranca la iteración."""
    if state.get("es_duplicada") or state.get("estado_final") == "ERROR":
        return "manejar_duplicada"
    if not state.get("items_pendientes"):
        return "registrar_auditoria"
    return "preparar_siguiente_item"


def decidir_ruta_evaluacion(state: AgentState) -> str:
    """Borde condicional 2: Desvía a revisión humana si hay incertidumbre o alzas de precio."""
    item = state.get("item_actual") or {}
    decision = state.get("decision_actual") or {}
    alerta = state.get("alerta_precio") or {}

    sku = decision.get("sku_seleccionado")
    confianza = float(decision.get("confianza") or 0.0)
    requiere_revision = bool(decision.get("requiere_revision", False))
    hay_alerta_precio = bool(alerta.get("alerta", False))
    campos_ilegibles = _obtener_campos_ilegibles(item)

    if not sku or requiere_revision or confianza < 0.75 or hay_alerta_precio or len(campos_ilegibles) > 0:
        return "intervencion_humana"
    return "aplicar_cambios"


def decidir_ruta_siguiente_item(state: AgentState) -> str:
    """Borde condicional 3 (Loop): Si quedan más ítems pendientes, cicla de regreso."""
    pendientes = state.get("items_pendientes", [])
    if len(pendientes) > 0:
        return "preparar_siguiente_item"
    return "registrar_auditoria"


# ============================================================================
# 3. ENSAMBLAJE DEL GRAFO (LangGraph StateGraph)
# ============================================================================

def crear_grafo_agente(checkpointer: Optional[Any] = None):
    """
    Construye y compila el StateGraph del agente.
    Por defecto compila sin checkpointer en memoria para evitar acumulación ilimitada de RAM
    en el Worker asíncrono (diseño Stateless). Se puede suministrar un checkpointer si se requiere reanudar estados.
    """
    builder = StateGraph(AgentState)

    # Registrar Nodos
    builder.add_node("extraer", node_extraer)
    builder.add_node("verificar_duplicidad", node_verificar_duplicidad)
    builder.add_node("manejar_duplicada", node_manejar_duplicada)
    builder.add_node("preparar_siguiente_item", node_preparar_siguiente_item)
    builder.add_node("recuperar_candidatos", node_recuperar_candidatos)
    builder.add_node("razonar_homologacion", node_razonar_homologacion)
    builder.add_node("evaluar_costo", node_evaluar_costo)
    builder.add_node("intervencion_humana", node_intervencion_humana)
    builder.add_node("aplicar_cambios", node_aplicar_cambios)
    builder.add_node("registrar_auditoria", node_registrar_auditoria)

    # Conectar Bordes
    builder.add_edge(START, "extraer")
    builder.add_edge("extraer", "verificar_duplicidad")

    # Bifurcación 1
    builder.add_conditional_edges(
        "verificar_duplicidad",
        decidir_ruta_duplicidad,
        {
            "manejar_duplicada": "manejar_duplicada",
            "preparar_siguiente_item": "preparar_siguiente_item",
            "registrar_auditoria": "registrar_auditoria"
        }
    )
    builder.add_edge("manejar_duplicada", END)

    # Ciclo de Ítem
    builder.add_edge("preparar_siguiente_item", "recuperar_candidatos")
    builder.add_edge("recuperar_candidatos", "razonar_homologacion")
    builder.add_edge("razonar_homologacion", "evaluar_costo")

    # Bifurcación 2
    builder.add_conditional_edges(
        "evaluar_costo",
        decidir_ruta_evaluacion,
        {
            "intervencion_humana": "intervencion_humana",
            "aplicar_cambios": "aplicar_cambios"
        }
    )
    builder.add_edge("intervencion_humana", "aplicar_cambios")

    # Bifurcación 3 (Loop)
    builder.add_conditional_edges(
        "aplicar_cambios",
        decidir_ruta_siguiente_item,
        {
            "preparar_siguiente_item": "preparar_siguiente_item",
            "registrar_auditoria": "registrar_auditoria"
        }
    )
    builder.add_edge("registrar_auditoria", END)

    if checkpointer is not None:
        return builder.compile(checkpointer=checkpointer)
    return builder.compile()

