"""Validación del encabezado financiero sin inferir montos ausentes."""
from datetime import date
from decimal import Decimal, InvalidOperation
import re

CAMPOS_MONTOS = ("monto_neto", "monto_impuestos", "monto_exento", "total_factura")
CAMPOS_FINANCIEROS = (
    "fecha_emision", "rut_proveedor", "moneda", *CAMPOS_MONTOS,
    "monto_iva", "impuestos_adicionales",
    "estado_validacion", "motivos_revision", "version_financiera",
)


def monto_decimal(valor):
    if valor is None or isinstance(valor, bool):
        return None
    try:
        numero = Decimal(str(valor))
        return numero if numero.is_finite() and numero >= 0 else None
    except (InvalidOperation, ValueError):
        return None


def fecha_iso(valor):
    if not isinstance(valor, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", valor):
        return None
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError:
        return None


def normalizar_rut(valor):
    return re.sub(r"[^0-9K]", "", str(valor or "").upper()) or None


def validar_finanzas(datos):
    """Comprueba presencia y coherencia aritmética; no certifica validez tributaria."""
    resultado = {campo: monto_decimal(datos.get(campo)) for campo in CAMPOS_MONTOS}
    resultado.update(
        fecha_emision=fecha_iso(datos.get("fecha_emision")),
        rut_proveedor=normalizar_rut(datos.get("rut_proveedor")),
        moneda=str(datos.get("moneda") or "").strip().upper() or None,
        version_financiera=1,
    )
    motivos = []
    def motivo(codigo, mensaje):
        motivos.append({"codigo": codigo, "mensaje": mensaje})
    resultado["monto_iva"] = monto_decimal(datos.get("monto_iva"))
    adicionales = datos.get("impuestos_adicionales")
    resultado["impuestos_adicionales"] = None
    if isinstance(adicionales, list):
        detalle = []
        for impuesto in adicionales:
            if not isinstance(impuesto, dict):
                motivo("IMPUESTO_ADICIONAL_INVALIDO", "Hay un impuesto adicional con formato inválido.")
                detalle.append({"nombre": "Impuesto ilegible", "codigo": None, "tasa": None, "monto": None})
                continue
            linea = {
                "nombre": str(impuesto.get("nombre") or "Impuesto adicional")[:200],
                "codigo": str(impuesto["codigo"])[:30] if impuesto.get("codigo") is not None else None,
                "tasa": str(impuesto["tasa"])[:30] if impuesto.get("tasa") is not None else None,
                "monto": monto_decimal(impuesto.get("monto")),
            }
            if linea["monto"] is None:
                motivo("IMPUESTO_ADICIONAL_ILEGIBLE", f"Falta un monto válido para {linea['nombre']}.")
            detalle.append(linea)
        resultado["impuestos_adicionales"] = detalle
        if resultado["monto_iva"] is not None and all(i["monto"] is not None for i in detalle):
            # Suma exclusivamente importes leídos; nunca infiere impuestos desde tasas ni desde el total.
            resultado["monto_impuestos"] = resultado["monto_iva"] + sum((i["monto"] for i in detalle), Decimal(0))
        elif detalle or datos.get("monto_iva") is not None:
            motivo("DESGLOSE_IMPUESTOS_INCOMPLETO", "Completa IVA y todos los montos adicionales antes de validar el desglose.")
    elif adicionales is not None:
        motivo("DESGLOSE_IMPUESTOS_INVALIDO", "El desglose de impuestos adicionales debe ser una lista.")
    elif datos.get("monto_iva") is not None:
        motivo("DESGLOSE_IMPUESTOS_INCOMPLETO", "Indica los impuestos adicionales o una lista vacía si no hay adicionales.")
    if not resultado["fecha_emision"]:
        motivo("FECHA_AUSENTE_O_INVALIDA", "La fecha de emisión falta o no es válida.")
    elif date.fromisoformat(resultado["fecha_emision"]) > date.today():
        motivo("FECHA_FUTURA", "La fecha de emisión está en el futuro.")
    proveedor = str(datos.get("proveedor") or "").strip()
    if proveedor.lower() in ("", "desconocido", "pendiente_extraccion", "null", "none"):
        motivo("PROVEEDOR_AUSENTE", "No se pudo identificar al proveedor.")
    if not re.fullmatch(r"[A-Z]{3}", resultado["moneda"] or ""):
        motivo("MONEDA_AUSENTE_O_INVALIDA", "Debe indicarse una moneda de tres letras, por ejemplo CLP.")
    for campo in CAMPOS_MONTOS:
        if resultado[campo] is None:
            motivo(campo.upper() + "_AUSENTE_O_INVALIDO", f"El campo {campo} falta o es inválido.")
    if all(resultado[campo] is not None for campo in CAMPOS_MONTOS):
        suma = sum((resultado[campo] for campo in CAMPOS_MONTOS[:-1]), Decimal(0))
        tolerancia = Decimal(1) if resultado["moneda"] == "CLP" else Decimal("0.01")
        if abs(suma - resultado["total_factura"]) > tolerancia:
            diferencia = resultado["total_factura"] - suma
            motivo("TOTALES_NO_COINCIDEN", f"Neto + impuestos (IVA y adicionales) + exento = {suma}; total leído = {resultado['total_factura']}; diferencia = {diferencia}. Revisa los montos de la imagen.")
    resultado["estado_validacion"] = "REQUIERE_REVISION" if motivos else "VALIDADO"
    resultado["motivos_revision"] = motivos
    return resultado
