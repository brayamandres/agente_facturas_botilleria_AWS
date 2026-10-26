# Estadísticas y asistente de facturas

## Puesta en marcha

Desde la raíz del repositorio:

```powershell
docker compose up --build -d
```

Abrir `http://localhost:8000/estadisticas` o usar el nuevo enlace del menú.
Los Dockerfiles ahora se construyen desde la raíz para incluir `shared/`:

```powershell
docker build -f api/Dockerfile -t botilleria-api .
docker build -f worker/Dockerfile -t botilleria-worker .
```

El módulo compartido solo contiene contratos y validación; la API no importa modelos de embeddings ni LangGraph.

## Datos y reglas

Gemini extrae fecha de emisión, moneda, RUT del proveedor, neto, impuestos, exento y total final. Las cantidades monetarias se conservan con `Decimal`. Los valores ausentes son `null`; no se reconstruye el gasto a partir de precios actuales del catálogo.

El IVA se conserva en `monto_iva`. Cada impuesto adicional del resumen se guarda en `impuestos_adicionales` con nombre, código, tasa impresa y monto. Si el desglose está completo, el sistema suma sus importes al IVA para obtener `monto_impuestos`; no deduce montos desde tasas ni de la diferencia con el total. La revisión manual incluye un desglose editable y la discrepancia aritmética exacta. Los registros anteriores no se reextraen automáticamente: pueden completarse desde **Revisar** sin mover stock.

- El período se aplica a la fecha de emisión, con ambos extremos incluidos. Inicialmente muestra los últimos tres meses hasta hoy.
- El gasto suma encabezados validados de facturas que finalizaron el procesamiento, incluso si un producto está pendiente de homologación. La validación financiera es independiente del estado del inventario.
- Se verifica presencia, formato y coherencia de neto + impuestos + exento con el total (tolerancia de 1 CLP o 0,01 para otras monedas). Los documentos con otros ajustes quedan para revisión. No se certifica validez tributaria ni se valida el dígito verificador del RUT.
- Se excluyen estados en curso, errores completos y duplicados. Se deduplica por identidad del proveedor y folio, prefiriendo la copia financiera válida.
- No se mezclan monedas. Los documentos sin moneda se muestran como incompletos dentro del período. El selector inicial permite CLP, USD y EUR; la API acepta códigos de tres letras.
- Las facturas sin fecha aparecen en una bandeja del historial general. No se atribuyen a un mes ni se suman al período.
- Los meses sin compras válidas aparecen con cero. Una falla de DynamoDB se muestra como error, nunca como gasto cero.
- **Gasto por producto** usa barras horizontales ordenadas de mayor a menor (Top 10, Top 20 o todos), con el mismo período, moneda y deduplicación. Agrupa por SKU; sin SKU utiliza glosa y formato. Incluye líneas de facturas con encabezado financiero válido, incluso si un producto fue omitido del inventario. Prioriza `monto_linea_neto` impreso; si falta, estima cantidad × precio registrado únicamente cuando su base es conocida (unidad/bulto) o se trata de unidades individuales. Las estimaciones se marcan con asterisco y se informa cuántas líneas se omitieron. No reparte impuestos globales ni cargos: el desglose puede no coincidir con el total de compras y las estimaciones pueden no reflejar descuentos. Los registros históricos de packs ambiguos requieren datos adicionales para incluirse; no se modifican ni reprocesan automáticamente.
- La API pagina los scans hasta completar el historial. Este diseño es adecuado para el piloto; para alto volumen se deben agregar índices y agregados por período.
- El CSV usa el mismo filtro e incluye una columna `incluida_en_gasto`, permitiendo reconciliar la suma. Las facturas sin fecha se revisan aparte y no se exportan como parte de un período.

## Completar facturas antiguas sin mover stock

Los registros previos sin `version_financiera` no se consideran encabezados validados. Un cero antiguo puede ser un marcador de dato ausente.

En Estadísticas, abrir **Revisar**, consultar la imagen original y completar el encabezado. Se guardan solo campos financieros y el proveedor en la tabla de historial; no se envían eventos ni se altera el inventario. Puede corregirse una factura con ítems pendientes sin resolver esos ítems.

También existe una herramienta local para un JSON revisado:

```json
{
  "proveedor": "Distribuidora Ejemplo",
  "rut_proveedor": null,
  "fecha_emision": "2025-10-25",
  "moneda": "CLP",
  "monto_neto": 100000,
  "monto_impuestos": 19000,
  "monto_exento": 0,
  "total_factura": 119000
}
```

```powershell
python -m worker.src.services.completar_finanzas --factura-id ID --archivo encabezado.json
python -m worker.src.services.completar_finanzas --factura-id ID --archivo encabezado.json --aplicar
```

El primer comando muestra una vista previa sin consultar AWS. El segundo actualiza exclusivamente el registro existente de historial, con las credenciales configuradas. No ejecutar el grafo completo para actualizar encabezados históricos.

## Asistente

`POST /api/asistente` recibe pregunta, desde, hasta y moneda. El período y la moneda los controla la pantalla; se incluyen explícitamente en las respuestas. La pregunta no cambia esos filtros.

Ejemplos: «¿Cuánto gasté por proveedor?», «¿Cuánto gasté con Distribuidora Ejemplo?», «¿Qué facturas están por revisar?» y «Detalle de la factura 12345».

Las consultas de detalle muestran el encabezado financiero, IVA e impuestos adicionales, estado y hasta 20 productos en el chat. El enlace de la factura abre el formulario y todas las líneas guardadas, incluidos cargos. Si no se conservaron productos en el historial, se informa explícitamente; no se reconstruyen desde el catálogo. `GET /api/facturas/{id}/detalle` entrega este detalle de solo lectura.

El modelo clasifica la consulta entre resumen, proveedor, pendientes y detalle. Python valida el plan, lee datos y calcula la respuesta. No se generan consultas ni código ejecutable y no hay herramientas de escritura. Las referencias permiten abrir las facturas que respaldan el resultado; se muestran hasta 50, sin recortar los cálculos.

Se utiliza `GROQ_API_KEY` si está configurada; en su defecto `OPENAI_API_KEY`. `ASSISTANT_MODEL` permite seleccionar un modelo compatible del proveedor elegido. El cliente usa JSON mode y vuelve a validar la salida en Python ([documentación oficial de OpenAI](https://developers.openai.com/api/docs/guides/structured-outputs?api-mode=chat)).

Sin clave o ante un fallo del proveedor, se conservan las consultas locales básicas, indicando ese modo al usuario. El modelo recibe la pregunta, no imágenes ni el historial completo; las cifras y referencias se generan localmente. La conversación visible no se persiste ni se envía como memoria entre preguntas; cada consulta usa el período actual.

## Endpoints

| Método y ruta | Función |
| --- | --- |
| `GET /estadisticas` | Interfaz responsive |
| `GET /api/estadisticas?desde=YYYY-MM-DD&hasta=YYYY-MM-DD&moneda=CLP` | Resumen, series y facturas |
| `GET /api/estadisticas/exportar.csv` | Mismos parámetros; exportación |
| `POST /api/asistente` | Consulta de solo lectura |
| `PATCH /api/facturas/{id}/finanzas` | Reemplazo del encabezado financiero revisado; no toca inventario |

## Verificación

Con pytest y las dependencias de API instaladas:

```powershell
python -m pytest tests/test_estadisticas.py -q
```

Prueba visual opcional con Playwright y Chromium instalados:

```powershell
$env:RUN_UI_TESTS='1'
python -m pytest tests/test_estadisticas_ui.py -q
```

Las pruebas simulan AWS y los proveedores de IA. Cubren cálculo, duplicados, datos incompletos, paginación, CSV, fallos y edición financiera. La prueba visual cubre filtros, chat, corrección, descarga y ancho móvil.
