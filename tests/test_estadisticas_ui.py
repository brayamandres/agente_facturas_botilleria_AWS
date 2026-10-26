"""Prueba visual sin AWS ni llamadas de IA: RUN_UI_TESTS=1 python -m pytest tests/test_estadisticas_ui.py -q."""
import json
import os
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from api.services.estadisticas import calcular_estadisticas, exportar_csv, detalle_factura
from api.services.asistente import responder
from shared.finanzas import validar_finanzas


@pytest.mark.skipif(os.getenv("RUN_UI_TESTS") != "1", reason="Prueba visual opcional con Chromium instalado")
def test_pantalla_interacciones_y_responsive(monkeypatch):
    from playwright.sync_api import sync_playwright
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    raiz = Path(__file__).resolve().parents[1]
    hoy = date.today()
    base = dict(proveedor="Distribuidora Pacífico", fecha_emision=hoy.replace(day=1).isoformat(), moneda="CLP", monto_neto=100000, monto_impuestos=19000, monto_exento=0, total_factura=119000, estado="COMPLETADO", items=[{"glosa_original":"Pisco Mistral", "cantidad":2, "precio_unitario":1000, "sku_homologado":"PIS-001", "estado":"AUTO_HOMOLOGADO"}], version_financiera=1)
    registros = [{**base, "id": "uno", "folio": "101"}, {**base, "id": "dos", "folio": "102", "proveedor": "Comercial Los Andes"}, {**base, "id": "tres", "folio": "103", "total_factura": None}, {**base, "id": "cuatro", "folio": "104", "fecha_emision": None, "version_financiera": None}]
    estado = {"error": False}
    def interceptar(route):
        url = urlparse(route.request.url)
        path = url.path
        if path == "/estadisticas":
            from jinja2 import Environment, FileSystemLoader, select_autoescape
            plantillas = Environment(loader=FileSystemLoader(raiz/"api/templates"), autoescape=select_autoescape(["html"]))
            route.fulfill(content_type="text/html", body=plantillas.get_template("estadisticas.html").render())
        elif path.startswith("/static/"):
            archivo = raiz/"api"/path.lstrip("/")
            route.fulfill(content_type="text/css" if path.endswith(".css") else "application/javascript", body=archivo.read_text(encoding="utf-8"))
        elif path in {"/api/estadisticas", "/api/estadisticas/exportar.csv"}:
            if estado["error"]:
                route.fulfill(status=503, json={"detail": "DynamoDB no disponible"})
                return
            q = {k: v[0] for k,v in parse_qs(url.query).items()}
            datos = calcular_estadisticas(registros, **q)
            if path.endswith(".csv"):
                route.fulfill(content_type="text/csv", headers={"Content-Disposition": 'attachment; filename="facturas.csv"'}, body=exportar_csv(datos))
            else:
                route.fulfill(content_type="application/json", body=json.dumps(datos, default=float))
        elif path == "/api/asistente":
            q = route.request.post_data_json
            datos = responder(q.pop("pregunta"), registros, **q)
            route.fulfill(content_type="application/json", body=json.dumps(datos, default=float))
        elif path.endswith("/detalle"):
            id = path.split("/")[3]
            registro = next(f for f in registros if f["id"] == id)
            route.fulfill(content_type="application/json", body=json.dumps(detalle_factura(registro), default=float))
        elif path.endswith("/finanzas"):
            id = path.split("/")[3]
            registro = next(f for f in registros if f["id"] == id)
            payload = route.request.post_data_json
            registro.update(payload)
            registro.update(validar_finanzas(registro))
            route.fulfill(content_type="application/json", body=json.dumps(validar_finanzas(registro), default=float))
        else:
            route.fulfill(status=404, json={"detail": "No disponible en prueba"})
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1366, "height": 1000}, device_scale_factor=1)
        errores = []
        page.on("pageerror", lambda error: errores.append(str(error)))
        page.route("**/*", interceptar)
        page.goto("http://estadisticas.test/estadisticas")
        page.locator("#estado-carga").filter(has_text="Datos actualizados").wait_for()
        assert page.locator("#procesadas").inner_text() == "3"
        assert page.locator("#pendientes").inner_text() == "1"
        assert "238.000" in page.locator("#gasto").inner_text()
        assert "Pisco Mistral" in page.locator("#productos").inner_text()
        assert "4.000" in page.locator("#productos").inner_text()
        assert "2 líneas estimadas" in page.locator("#cobertura-productos").inner_text()
        page.locator("#limite-productos").select_option("0")
        page.get_by_role("button", name="Facturas por revisar", exact=True).click()
        page.locator("#chat .source-button").first.wait_for()
        assert "1 facturas por revisar" in page.locator("#chat").inner_text()
        page.locator("#pregunta").fill("Muéstrame el detalle de la factura 101")
        page.locator("#chat-form").get_by_role("button", name="Enviar", exact=True).click()
        page.locator("#chat .message.bot").last.filter(has_text="Pisco Mistral").wait_for()
        assert "Total factura:" in page.locator("#chat .message.bot").last.inner_text()
        page.locator("#chat .message.bot").last.locator(".source-button").click()
        page.locator("#detalle-lineas").filter(has_text="Pisco Mistral").wait_for()
        page.get_by_role("button", name="Cerrar", exact=True).click()
        page.locator("#sin-fecha button").click()
        page.locator('input[name="fecha_emision"]').fill(base["fecha_emision"])
        page.locator('input[name="total_factura"]').fill("119000")
        page.get_by_role("button", name="Guardar encabezado", exact=True).click()
        page.locator("#guardar-estado").filter(has_text="validado y guardado").wait_for()
        assert page.locator("#procesadas").inner_text() == "4"
        page.get_by_role("button", name="Cerrar", exact=True).click()
        with page.expect_download() as descarga:
            page.locator("#exportar").click()
        assert descarga.value.suggested_filename == "facturas.csv"
        # Un impuesto adicional se suma al IVA sin introducirlo manualmente en el total de impuestos.
        page.locator("#facturas button").first.click()
        page.locator("#usar-desglose").check()
        page.locator('input[name="monto_iva"]').fill("19000")
        page.get_by_role("button", name="+ Agregar impuesto adicional", exact=True).click()
        page.locator(".tax-nombre").fill("IMP.ADIC. COD. 27")
        page.locator(".tax-codigo").fill("27")
        page.locator(".tax-monto").fill("1000")
        assert page.locator('input[name="monto_impuestos"]').input_value() == "20000.00"
        page.locator('input[name="total_factura"]').fill("120000")
        page.get_by_role("button", name="Guardar encabezado", exact=True).click()
        page.locator("#guardar-estado").filter(has_text="validado y guardado").wait_for()
        assert "358.000" in page.locator("#gasto").inner_text()
        page.get_by_role("button", name="Cerrar", exact=True).click()
        destino = os.getenv("UI_SCREENSHOT_DIR")
        page.evaluate("window.scrollTo(0, 0)")
        if destino:
            Path(destino).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(destino)/"estadisticas-desktop.png"), full_page=True)
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("window.scrollTo(0, 0)")
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        if destino:
            page.screenshot(path=str(Path(destino)/"estadisticas-mobile.png"), full_page=True)
        estado["error"] = True
        page.get_by_role("button", name="Aplicar período", exact=True).click()
        page.locator("#estado-carga").filter(has_text="DynamoDB no disponible").wait_for()
        assert page.locator("#gasto").inner_text() == "—"
        assert page.locator("#productos").inner_text() == ""
        assert not errores
        browser.close()
