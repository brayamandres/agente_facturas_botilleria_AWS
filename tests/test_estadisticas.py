import csv
import ast
import io
import logging
import sys
import types
from pathlib import Path
from typing import Any, Dict, List, Optional
from decimal import Decimal
from unittest.mock import Mock, patch

import pytest

from shared.finanzas import validar_finanzas
from api.services.estadisticas import calcular_estadisticas, exportar_csv, scan_completo
from api.services.asistente import responder


def factura(id="a", **cambios):
    registro = dict(id=id, folio=id, proveedor="Distribuidora Uno", fecha_emision="2025-10-15", moneda="CLP", monto_neto=100, monto_impuestos=19, monto_exento=0, total_factura=119, estado="COMPLETADO", items=[], version_financiera=1)
    registro.update(cambios)
    return registro


def test_encabezado_valido_y_cero_real():
    assert validar_finanzas(factura())["estado_validacion"] == "VALIDADO"
    assert validar_finanzas(factura(monto_neto=0, monto_impuestos=0, total_factura=0))["total_factura"] == Decimal(0)


@pytest.mark.parametrize("campo,valor", [("total_factura", None), ("total_factura", "NaN"), ("total_factura", -1), ("total_factura", True), ("fecha_emision", "2025-02-30"), ("moneda", None), ("monto_exento", None)])
def test_campos_invalidos_no_se_validan(campo, valor):
    assert validar_finanzas(factura(**{campo: valor}))["estado_validacion"] == "REQUIERE_REVISION"


def test_totales_incoherentes():
    assert any(m["codigo"] == "TOTALES_NO_COINCIDEN" for m in validar_finanzas(factura(total_factura=200))["motivos_revision"])


def test_iva_y_adicionales_se_suman_sin_inferir_desde_total():
    registro = factura(monto_neto=429793, monto_iva=81661, monto_impuestos=81661, impuestos_adicionales=[
        {"codigo": "27", "monto": 4912}, {"codigo": "271", "monto": 5045},
        {"codigo": "25", "monto": 4573}, {"codigo": "24", "monto": 96576},
    ], total_factura=622560)
    validado = validar_finanzas(registro)
    assert validado["monto_impuestos"] == Decimal(192767)
    assert validado["estado_validacion"] == "VALIDADO"
    assert len(validado["impuestos_adicionales"]) == 4
    registro["total_factura"] = 622580
    inconsistente = validar_finanzas(registro)
    assert inconsistente["estado_validacion"] == "REQUIERE_REVISION"
    assert any("diferencia = 20" in m["mensaje"] for m in inconsistente["motivos_revision"])


def test_desglose_ilegible_no_se_completa_por_diferencia():
    registro = factura(monto_iva=19, impuestos_adicionales=[{"codigo": "27", "monto": None}])
    resultado = validar_finanzas(registro)
    assert resultado["estado_validacion"] == "REQUIERE_REVISION"
    assert resultado["impuestos_adicionales"][0]["monto"] is None


def test_iva_sin_adicionales_y_desglose_ausente():
    assert validar_finanzas(factura(monto_iva=19, impuestos_adicionales=[]))["estado_validacion"] == "VALIDADO"
    assert validar_finanzas(factura(monto_iva=19, impuestos_adicionales=None))["estado_validacion"] == "REQUIERE_REVISION"


def test_sumas_deduplicacion_estados_y_monedas():
    registros = [factura(), factura("b"), factura("duplicada", folio="a"), factura("usd", moneda="USD"), factura("error", estado="ERROR"), factura("duplicado", estado="DUPLICADO_IGNORADO")]
    datos = calcular_estadisticas(registros, "2025-10-01", "2025-10-31")
    assert datos["resumen"]["gasto_validado"] == 238
    assert datos["resumen"]["facturas_procesadas"] == 2
    assert datos["por_proveedor"][0]["facturas"] == 2


def test_pendientes_se_cuentan_por_factura_y_sin_fecha_es_global():
    registros = [factura(items=[{"estado": "REQUIERE_REVISION_MANUAL"}]*2), factura("sin-fecha", fecha_emision=None), factura("sin-total", total_factura=None), factura("otro-mes", fecha_emision="2025-09-01")]
    datos = calcular_estadisticas(registros, "2025-10-01", "2025-10-31")
    assert datos["resumen"]["facturas_por_revisar"] == 2
    assert datos["resumen"]["facturas_sin_fecha_global"] == 1
    assert datos["resumen"]["gasto_validado"] == 119


def test_legacy_no_convierte_cero_en_compra_y_meses_vacios():
    datos = calcular_estadisticas([factura(version_financiera=None, total_factura=0)], "2025-08-01", "2025-10-31")
    assert datos["resumen"]["gasto_validado"] == 0
    assert datos["facturas"][0]["total_factura"] is None
    assert len(datos["por_mes"]) == 3


def test_duplicado_incompleto_no_oculta_copia_validada_con_rut():
    datos = calcular_estadisticas([factura("a", total_factura=None), factura("b", folio="a", rut_proveedor="76.123.456-7")], "2025-10-01", "2025-10-31")
    assert datos["resumen"]["gasto_validado"] == 119
    assert datos["resumen"]["facturas_procesadas"] == 1


def test_precision_decimal_no_se_pierde_en_csv():
    datos = calcular_estadisticas([factura(moneda="USD", monto_neto=Decimal("999999999999.01"), monto_impuestos=0, total_factura=Decimal("999999999999.01"))], "2025-10-01", "2025-10-31", "USD")
    assert "999999999999.01" in exportar_csv(datos)


def test_gasto_producto_prioriza_importe_de_linea_y_agrupa_por_sku():
    registros = [factura(items=[
        {"glosa_original": "Pisco Mistral", "sku_homologado": "PIS", "cantidad": 2, "precio_unitario": 80, "monto_linea_neto": Decimal("120.25")},
        {"glosa_original": "Mistral abreviado", "sku_homologado": "PIS", "cantidad": 1, "precio_unitario": 50},
        {"glosa_original": "Whisky", "sku_homologado": "WHI", "monto_linea_neto": 200},
    ])]
    datos = calcular_estadisticas(registros, "2025-10-01", "2025-10-31")
    assert datos["por_producto"][0]["sku"] == "WHI"
    pisco = datos["por_producto"][1]
    assert pisco["total"] == Decimal("170.25")
    assert pisco["lineas"] == 2
    assert pisco["estimado"] is True
    assert datos["cobertura_productos"]["total_representado"] == Decimal("370.25")


def test_gasto_producto_no_multiplica_packs_ambiguos_y_respeta_base():
    datos = calcular_estadisticas([factura(items=[
        {"glosa_original": "Pack ambiguo", "cantidad": 144, "unidades_por_pack": 24, "precio_unitario": 341},
        {"glosa_original": "Pack precio por unidad", "cantidad": 144, "unidades_por_pack": 24, "precio_unitario": 341, "base_precio": "UNIDAD"},
        {"glosa_original": "Pack precio por bulto", "cantidad": 144, "cantidad_facturada": 6, "unidades_por_pack": 24, "precio_facturado": 8184, "base_precio": "BULTO"},
    ])], "2025-10-01", "2025-10-31")
    assert datos["cobertura_productos"]["lineas_omitidas"] == 1
    assert all(p["total"] == Decimal(49104) for p in datos["por_producto"])


def test_gasto_producto_excluye_cargos_invalidos_y_facturas_fuera_del_filtro():
    datos = calcular_estadisticas([
        factura(items=[{"glosa_original":"Flete", "tipo_item":"CARGO", "monto_linea_neto":10}, {"glosa_original":"Monto ilegible", "precio_unitario":10, "cantidad":1, "monto_linea_neto":-1}, {"glosa_original":"Muestra", "monto_linea_neto":0}]),
        factura("duplicada",folio="a",items=[{"glosa_original":"Duplicado", "monto_linea_neto":500}]),
        factura("fuera",fecha_emision="2025-09-01",items=[{"glosa_original":"Fuera", "monto_linea_neto":500}]),
        factura("usd",moneda="USD",items=[{"glosa_original":"USD", "monto_linea_neto":500}]),
    ], "2025-10-01", "2025-10-31")
    assert len(datos["por_producto"]) == 1
    assert datos["por_producto"][0]["producto"] == "Muestra"
    assert datos["cobertura_productos"]["lineas_omitidas"] == 1


def test_paginacion():
    tabla = Mock()
    tabla.scan.side_effect = [{"Items": [], "LastEvaluatedKey": {"id": "a"}}, {"Items": [factura()]}]
    assert len(scan_completo(tabla)) == 1
    assert tabla.scan.call_args.kwargs["ExclusiveStartKey"] == {"id": "a"}


def test_csv_cuadra_con_grafico_y_escapa_formulas():
    datos = calcular_estadisticas([factura(proveedor="=IMPORTDATA()"), factura("b")], "2025-10-01", "2025-10-31")
    filas = list(csv.DictReader(io.StringIO(exportar_csv(datos).lstrip("\ufeff"))))
    assert sum(Decimal(f["total_factura"]) for f in filas if f["incluida_en_gasto"] == "True") == datos["resumen"]["gasto_validado"]
    assert filas[0]["proveedor"].startswith("'")


def test_periodo_invalido():
    with pytest.raises(ValueError):
        calcular_estadisticas([], "2025-10-31", "2025-10-01")


def test_asistente_consultas_reales_sin_proveedor():
    with patch.dict("os.environ", {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
        respuesta = responder("¿Cuánto gasté con Distribuidora Uno?", [factura()], "2025-10-01", "2025-10-31", "CLP")
        assert "119,00" in respuesta["respuesta"]
        assert respuesta["facturas"][0]["id"] == "a"
        pendientes = responder("¿Qué facturas están por revisar?", [factura(total_factura=None)], "2025-10-01", "2025-10-31", "CLP")
        assert len(pendientes["facturas"]) == 1


def test_asistente_fallo_proveedor_y_plan_no_permitido():
    with patch.dict("os.environ", {"GROQ_API_KEY": "test"}), patch("httpx.Client") as client:
        client.return_value.__enter__.return_value.post.return_value.json.return_value = {"choices": [{"message": {"content": '{"intencion":"borrar_tabla"}'}}]}
        resultado = responder("Gasto del período", [factura()], "2025-10-01", "2025-10-31", "CLP")
        assert resultado["modo"] == "consultas_locales"
        assert "119,00" in resultado["respuesta"]


def test_asistente_detalle_incluye_encabezado_impuestos_y_productos():
    registro = factura(folio="9579", monto_iva=19, impuestos_adicionales=[], items=[{
        "glosa_original": "Pisco Mistral", "cantidad": 2, "precio_unitario": 50,
        "sku_homologado": "PIS-001", "estado": "AUTO_HOMOLOGADO",
    }])
    with patch.dict("os.environ", {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
        resultado = responder("Muéstrame el detalle de la factura 9579", [registro], "2025-10-01", "2025-10-31", "CLP")
    assert "Proveedor: Distribuidora Uno" in resultado["respuesta"]
    assert "Total factura: 119 CLP" in resultado["respuesta"]
    assert "IVA: 19 CLP" in resultado["respuesta"]
    assert "Pisco Mistral" in resultado["respuesta"]
    assert "PIS-001" in resultado["respuesta"]
    assert resultado["facturas"][0]["items"][0]["cantidad"] == 2


def test_asistente_detalle_sin_productos_no_inventa_lineas():
    with patch.dict("os.environ", {"GROQ_API_KEY": "", "OPENAI_API_KEY": ""}):
        resultado = responder("Detalle factura 9579", [factura(folio="9579")], "2025-10-01", "2025-10-31", "CLP")
    assert "No hay líneas de productos guardadas" in resultado["respuesta"]


@pytest.fixture
def api_cliente():
    # Se reemplaza solo el adaptador de AWS; se ejecutan las rutas FastAPI reales.
    config = types.ModuleType("api.config")
    for nombre in ("S3_BUCKET_NAME", "SQS_QUEUE_URL", "DYNAMODB_TABLE_PRODUCTOS", "DYNAMODB_TABLE_HISTORIAL"):
        setattr(config, nombre, "test")
    config.get_s3_client = Mock()
    config.get_sqs_client = Mock()
    config.get_dynamodb_resource = Mock()
    with patch.dict(sys.modules, {"api.config": config}):
        sys.modules.pop("api.main", None)
        from api.main import app
        from fastapi.testclient import TestClient
        tabla = config.get_dynamodb_resource.return_value.Table.return_value
        tabla.scan.return_value = {"Items": [factura()]}
        tabla.get_item.return_value = {"Item": factura()}
        yield TestClient(app), tabla
    sys.modules.pop("api.main", None)


def test_rutas_y_exportacion(api_cliente):
    cliente, _ = api_cliente
    assert cliente.get("/estadisticas").status_code == 200
    assert cliente.get("/static/estadisticas.js").status_code == 200
    params = dict(desde="2025-10-01", hasta="2025-10-31")
    datos = cliente.get("/api/estadisticas", params=params)
    assert datos.json()["resumen"]["gasto_validado"] == 119
    assert "119" in cliente.get("/api/estadisticas/exportar.csv", params=params).text
    assert cliente.get("/api/estadisticas", params={**params,"desde":"invalido"}).status_code == 422
    detalle = cliente.get("/api/facturas/a/detalle")
    assert detalle.status_code == 200
    assert detalle.json()["folio"] == "a"
    assert "items" in detalle.json()


def test_edicion_financiera_solo_historial(api_cliente):
    cliente, tabla = api_cliente
    f = factura()
    payload = {k: f[k] for k in ("proveedor", "fecha_emision", "moneda", "monto_neto", "monto_impuestos", "monto_exento", "total_factura")}
    respuesta = cliente.patch("/api/facturas/a/finanzas", json=payload)
    assert respuesta.status_code == 200
    assert respuesta.json()["estado_validacion"] == "VALIDADO"
    assert tabla.update_item.call_count == 1
    campos = tabla.update_item.call_args.kwargs["ExpressionAttributeNames"].values()
    assert "stock_actual" not in campos
    assert "items" not in campos


def test_dynamo_no_disponible_no_devuelve_ceros(api_cliente):
    cliente, tabla = api_cliente
    tabla.scan.side_effect = RuntimeError("offline")
    assert cliente.get("/api/estadisticas?desde=2025-10-01&hasta=2025-10-31").status_code == 503


def test_grafo_transmite_finanzas_y_no_envia_cargos_a_inventario():
    """Ejecuta nodos reales aislando llamadas externas y dependencias pesadas."""
    ruta = Path(__file__).resolve().parents[1]/"worker/src/agent/graph.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    nodos = [n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name in {"node_extraer", "node_registrar_auditoria"}]
    from shared.finanzas import CAMPOS_FINANCIEROS
    auditoria = Mock()
    auditoria.invoke.return_value = {"exito": True, "id_auditoria": "a"}
    extraccion = Mock()
    extraccion.invoke.return_value = {**factura(), "numero_factura": "123", "items": [{"tipo_item": "PRODUCTO", "nombre_glosa": "Pisco"}, {"tipo_item": "CARGO", "nombre_glosa": "Flete"}]}
    scope = dict(AgentState=dict, Dict=Dict, Any=Any, logger=logging.getLogger("test"), validar_finanzas=validar_finanzas, CAMPOS_FINANCIEROS=CAMPOS_FINANCIEROS, extraer_factura_desde_imagen=extraccion, _registrar_progreso_en_dynamodb=Mock(), registrar_auditoria_factura=auditoria, _limpiar_texto=lambda x: str(x or "").strip())
    exec(compile(ast.Module(body=nodos, type_ignores=[]), str(ruta), "exec"), scope)
    estado = {"ruta_imagen": "fake.png", "factura_id": "a", "s3_key": "raw/a.png", "mensajes_bitacora": []}
    estado.update(scope["node_extraer"](estado))
    assert len(estado["items_pendientes"]) == 1
    scope["node_registrar_auditoria"](estado)
    payload = auditoria.invoke.call_args.args[0]
    assert payload["datos_financieros"]["fecha_emision"] == "2025-10-15"
    assert payload["total_factura"] == Decimal(119)
    assert len(payload["cargos"]) == 1


def test_auditoria_conserva_montos_exactos_y_ausentes():
    from datetime import datetime, timezone
    import uuid
    ruta = Path(__file__).resolve().parents[1]/"worker/src/tools/write_tools.py"
    arbol = ast.parse(ruta.read_text(encoding="utf-8"))
    nodo = next(n for n in arbol.body if isinstance(n, ast.FunctionDef) and n.name == "registrar_auditoria_factura")
    nodo.decorator_list = []
    guardar = Mock(return_value={"exito": True})
    scope = dict(Dict=Dict, Any=Any, List=List, Optional=Optional, Decimal=Decimal, datetime=datetime, timezone=timezone, uuid=uuid, validar_finanzas=validar_finanzas, logger=logging.getLogger("test"), guardar_auditoria_factura_dynamo=guardar)
    exec(compile(ast.Module(body=[nodo],type_ignores=[]), str(ruta), "exec"), scope)
    scope["registrar_auditoria_factura"]("123", "Proveedor", 0, [], total_factura=None)
    assert guardar.call_args.args[0]["total_factura"] is None
    scope["registrar_auditoria_factura"]("123", "Proveedor", 0, [], total_factura=Decimal("100000000000.01"))
    assert guardar.call_args.args[0]["total_factura"] == Decimal("100000000000.01")
