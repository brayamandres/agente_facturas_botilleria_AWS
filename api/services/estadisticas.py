"""Agregaciones financieras deterministas y lecturas completas de DynamoDB."""
from collections import defaultdict
from datetime import date
from decimal import Decimal
import csv
import io
import unicodedata

from shared.finanzas import fecha_iso, normalizar_rut, validar_finanzas, monto_decimal

ESTADOS_PROCESADOS = {"COMPLETADO", "REQUIERE_REVISION_MANUAL", "COMPLETADO_CON_ERRORES"}
ESTADOS_EXCLUIDOS = {"DUPLICADO_IGNORADO", "ERROR", "EN_COLA", "ANALIZANDO", "EN_PROCESO"}


def scan_completo(tabla, **kwargs):
    items = []
    while True:
        pagina = tabla.scan(**kwargs)
        items.extend(pagina.get("Items", []))
        ultima = pagina.get("LastEvaluatedKey")
        if not ultima:
            return items
        kwargs["ExclusiveStartKey"] = ultima


def texto_normalizado(valor):
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", str(valor or "").lower()) if not unicodedata.combining(c)).split())


def validar_periodo(desde, hasta):
    inicio, fin = fecha_iso(desde), fecha_iso(hasta)
    if not inicio or not fin or inicio > fin:
        raise ValueError("Indica un rango válido YYYY-MM-DD; desde no puede ser posterior a hasta.")
    if (date.fromisoformat(fin) - date.fromisoformat(inicio)).days > 3660:
        raise ValueError("El rango máximo es de diez años.")
    return inicio, fin


def preparar_facturas(registros):
    """No acepta ceros heredados como evidencia de un encabezado extraído."""
    salida = []
    vistos = set()
    # Prefiere una copia validada si existe otro registro de la misma factura.
    ordenados = sorted(registros, key=lambda x: (
        not (x.get("version_financiera") and validar_finanzas(x)["estado_validacion"] == "VALIDADO"),
        str(x.get("id", "")),
    ))
    ruts_por_nombre = defaultdict(set)
    for registro in registros:
        rut = normalizar_rut(registro.get("rut_proveedor"))
        if rut:
            ruts_por_nombre[texto_normalizado(registro.get("proveedor"))].add(rut)
    for original in ordenados:
        if original.get("estado") not in ESTADOS_PROCESADOS:
            continue
        f = dict(original)
        financiero = validar_finanzas(f)
        if not f.get("version_financiera"):
            if financiero["total_factura"] == 0:
                financiero["total_factura"] = None
            financiero["estado_validacion"] = "REQUIERE_REVISION"
            financiero["motivos_revision"].append({"codigo": "HISTORICO_SIN_VALIDAR", "mensaje": "Factura anterior: completar y validar encabezado financiero."})
        folio = texto_normalizado(f.get("folio"))
        proveedor = texto_normalizado(f.get("proveedor"))
        conocidos = ruts_por_nombre[proveedor]
        identidad = normalizar_rut(f.get("rut_proveedor")) or (next(iter(conocidos)) if len(conocidos) == 1 else proveedor)
        if folio and folio not in {"s/f", "n/a", "null", "none"} and identidad:
            clave = (identidad, folio)
            if clave in vistos:
                continue
            vistos.add(clave)
        f.update(financiero)
        f["requiere_revision"] = (
            financiero["estado_validacion"] != "VALIDADO"
            or f.get("estado") in {"REQUIERE_REVISION_MANUAL", "COMPLETADO_CON_ERRORES"}
            or any(i.get("estado") in {"REQUIERE_REVISION_MANUAL", "ERROR_ESCRITURA"} for i in f.get("items", []))
        )
        if any(i.get("estado") == "REQUIERE_REVISION_MANUAL" for i in f.get("items", [])):
            f["motivos_revision"].append({"codigo": "ITEMS_PENDIENTES", "mensaje": "Hay productos pendientes de revisión humana."})
        if f.get("estado") == "COMPLETADO_CON_ERRORES":
            f["motivos_revision"].append({"codigo": "ERROR_PROCESAMIENTO", "mensaje": "El procesamiento registró errores de escritura."})
        salida.append(f)
    return salida


def seleccionar_facturas(registros, desde, hasta, moneda="CLP", proveedor=None):
    desde, hasta = validar_periodo(desde, hasta)
    facturas = preparar_facturas(registros)
    # Las fechas ausentes se muestran en una bandeja global, nunca se atribuyen al período.
    sin_fecha = [f for f in facturas if not f.get("fecha_emision")]
    seleccion = [f for f in facturas if f.get("fecha_emision") and desde <= f["fecha_emision"] <= hasta]
    if proveedor:
        consulta = texto_normalizado(proveedor)
        seleccion = [f for f in seleccion if consulta in texto_normalizado(f.get("proveedor")) or normalizar_rut(proveedor) == f.get("rut_proveedor") and f.get("rut_proveedor")]
    seleccion = [f for f in seleccion if not f.get("moneda") or f["moneda"] == moneda]
    return seleccion, sin_fecha


def referencia(f):
    return {k: f.get(k) for k in ("id", "folio", "proveedor", "rut_proveedor", "fecha_emision", "moneda", "monto_neto", "monto_impuestos", "monto_iva", "impuestos_adicionales", "monto_exento", "total_factura", "estado", "estado_validacion", "motivos_revision", "requiere_revision")}


def detalle_factura(registro):
    normalizadas = preparar_facturas([registro])
    factura = normalizadas[0] if normalizadas else {**registro, **validar_finanzas(registro)}
    return {
        **referencia(factura),
        "items": registro.get("items") or [],
        "cargos": registro.get("cargos") or [],
        "fecha_procesamiento": registro.get("fecha_procesamiento"),
    }


def gasto_por_producto(facturas):
    """Usa líneas históricas, sin precios del catálogo ni distribución de impuestos."""
    grupos = {}
    incluidas = estimadas = omitidas = 0
    for factura in facturas:
        for item in factura.get("items") or []:
            if str(item.get("tipo_item") or "PRODUCTO").upper() == "CARGO":
                continue
            nombre = str(item.get("glosa_original") or item.get("nombre_glosa") or "").strip()
            if texto_normalizado(nombre) in {"", "ilegible", "none", "null"}:
                omitidas += 1
                continue
            importe = monto_decimal(item.get("monto_linea_neto"))
            estimado = False
            if importe is None:
                # Un importe explícito inválido no se reemplaza silenciosamente con una estimación.
                if item.get("monto_linea_neto") is not None:
                    omitidas += 1
                    continue
                precio = monto_decimal(item.get("precio_facturado", item.get("precio_unitario")))
                base = str(item.get("base_precio") or "").upper()
                unidades_pack = monto_decimal(item.get("unidades_por_pack", 1))
                if base == "BULTO":
                    cantidad = monto_decimal(item.get("cantidad_facturada"))
                elif base == "UNIDAD":
                    cantidad = monto_decimal(item.get("cantidad"))
                elif unidades_pack == 1:
                    cantidad = monto_decimal(item.get("cantidad"))
                else:
                    cantidad = None
                if cantidad is None or cantidad <= 0 or precio is None or precio <= 0:
                    omitidas += 1
                    continue
                importe = cantidad * precio
                estimado = True
            sku = str(item.get("sku_homologado") or "").strip() or None
            formato = str(item.get("formato") or "").strip()
            clave = ("sku", sku) if sku else ("glosa", texto_normalizado(nombre), texto_normalizado(formato))
            grupo = grupos.setdefault(clave, {"producto": nombre, "sku": sku, "formato": formato,
                "total": Decimal(0), "lineas": 0, "lineas_estimadas": 0, "facturas_ids": set()})
            grupo["total"] += importe
            grupo["lineas"] += 1
            grupo["lineas_estimadas"] += int(estimado)
            grupo["facturas_ids"].add(str(factura.get("id")))
            incluidas += 1
            estimadas += int(estimado)
    salida = []
    for grupo in grupos.values():
        grupo["facturas"] = len(grupo.pop("facturas_ids"))
        grupo["estimado"] = grupo["lineas_estimadas"] > 0
        salida.append(grupo)
    return sorted(salida, key=lambda g: (-g["total"], texto_normalizado(g["producto"]))), {
        "lineas_incluidas": incluidas, "lineas_estimadas": estimadas, "lineas_omitidas": omitidas,
        "total_representado": sum((g["total"] for g in salida), Decimal(0)),
    }


def calcular_estadisticas(registros, desde, hasta, moneda="CLP", proveedor=None):
    facturas, sin_fecha = seleccionar_facturas(registros, desde, hasta, moneda, proveedor)
    validas = [f for f in facturas if f["estado_validacion"] == "VALIDADO"]
    por_producto, cobertura_productos = gasto_por_producto(validas)
    por_proveedor = {}
    por_mes = defaultdict(lambda: {"total": Decimal(0), "facturas": 0})
    # Incluye meses sin compras para no ocultar huecos en el gráfico.
    cursor = date.fromisoformat(desde).replace(day=1)
    while cursor.isoformat()[:7] <= hasta[:7]:
        por_mes[cursor.isoformat()[:7]]
        cursor = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
    for f in validas:
        key = f.get("rut_proveedor") or texto_normalizado(f.get("proveedor"))
        grupo = por_proveedor.setdefault(key, {"proveedor": f["proveedor"], "rut_proveedor": f.get("rut_proveedor"), "total": Decimal(0), "facturas": 0})
        grupo["total"] += f["total_factura"]
        grupo["facturas"] += 1
        mes = por_mes[f["fecha_emision"][:7]]
        mes["total"] += f["total_factura"]
        mes["facturas"] += 1
    return {
        "periodo": {"desde": desde, "hasta": hasta}, "moneda": moneda,
        "resumen": {
            "gasto_validado": sum((f["total_factura"] for f in validas), Decimal(0)),
            "facturas_procesadas": len(facturas),
            "facturas_por_revisar": sum(bool(f["requiere_revision"]) for f in facturas),
            "facturas_sin_datos_suficientes": sum(f["estado_validacion"] != "VALIDADO" for f in facturas),
            "facturas_sin_fecha_global": len(sin_fecha),
        },
        "por_proveedor": sorted(por_proveedor.values(), key=lambda x: x["total"], reverse=True),
        "por_producto": por_producto,
        "cobertura_productos": cobertura_productos,
        "por_mes": [{"mes": mes, **valores} for mes, valores in sorted(por_mes.items())],
        "facturas": [referencia(f) for f in facturas],
        "sin_fecha": [referencia(f) for f in sin_fecha],
    }


def exportar_csv(estadisticas):
    salida = io.StringIO(newline="")
    campos = ["id", "folio", "fecha_emision", "proveedor", "rut_proveedor", "moneda", "total_factura", "estado", "estado_validacion", "incluida_en_gasto"]
    writer = csv.DictWriter(salida, fieldnames=campos)
    writer.writeheader()
    for f in estadisticas["facturas"]:
        fila = {k: f.get(k) for k in campos}
        fila["incluida_en_gasto"] = f["estado_validacion"] == "VALIDADO"
        # Evita que texto de OCR se interprete como fórmula al abrir el CSV.
        fila = {k: "'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for k, v in fila.items()}
        writer.writerow(fila)
    return "\ufeff" + salida.getvalue()
