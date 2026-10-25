"""
Herramientas de Razonamiento (Reasoning Tools) para el agente LangGraph.
Implementan razonamiento estructurado con LLM sobre glosas comerciales de licores en Chile,
validación estricta de esquemas Pydantic y análisis de variación de costos (>15%).
"""
import json
import logging
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser

from worker.src.services.llm import get_reasoning_llm

logger = logging.getLogger("worker.tools.reasoning_tools")


class ResultadoHomologacion(BaseModel):
    """Esquema estructurado y validado con Pydantic para la decisión de homologación."""
    sku_seleccionado: Optional[str] = Field(default=None, description="SKU del candidato seleccionado o None")
    confianza: float = Field(default=0.0, ge=0.0, le=1.0, description="Nivel de confianza entre 0.0 y 1.0")
    justificacion: str = Field(default="", description="Motivo analítico de la decisión")
    requiere_revision: bool = Field(default=True, description="Indica si requiere intervención humana (HITL)")


def validar_respuesta_homologacion(
    decision: Any,
    candidatos: Optional[List[Dict[str, Any]]] = None,
    umbral_confianza: float = 0.75
) -> Dict[str, Any]:
    """
    Valida estrictamente que la respuesta del LLM cumpla tipos, rangos y que el SKU seleccionado
    pertenezca efectivamente a los candidatos recuperados desde DynamoDB.
    El criterio del LLM tiene el 100% del peso en la confianza de homologación.
    """
    if not isinstance(decision, dict):
        return {
            "sku_seleccionado": None,
            "confianza": 0.0,
            "justificacion": "Respuesta del modelo con formato JSON inválido.",
            "requiere_revision": True
        }

    # 1. Normalizar SKU
    sku_raw = decision.get("sku_seleccionado")
    sku: Optional[str] = None
    if isinstance(sku_raw, str):
        sku_limpio = sku_raw.strip()
        if sku_limpio and sku_limpio.lower() not in ["null", "none", "n/a", "ninguno"]:
            sku = sku_limpio

    # 2. Normalizar justificación
    just_raw = decision.get("justificacion")
    justificacion = str(just_raw).strip() if just_raw is not None else ""
    if not justificacion:
        justificacion = "Sin justificación proporcionada."

    # 3. Normalizar confianza en rango [0.0, 1.0] (100% peso del LLM)
    conf_raw = decision.get("confianza", 0.0)
    try:
        confianza = float(conf_raw) if conf_raw is not None and not isinstance(conf_raw, bool) else 0.0
    except (ValueError, TypeError):
        confianza = 0.0
    confianza = max(0.0, min(1.0, confianza))

    # 4. Normalizar requiere_revision
    rev_raw = decision.get("requiere_revision", False)
    if isinstance(rev_raw, bool):
        requiere_revision = rev_raw
    elif isinstance(rev_raw, str):
        requiere_revision = rev_raw.strip().lower() in ["true", "1", "si", "sí", "yes"]
    else:
        requiere_revision = bool(rev_raw)

    # 5. Validar pertenencia obligatoria del SKU a los candidatos recuperados de DynamoDB
    lista_candidatos = candidatos if isinstance(candidatos, list) else []
    mapa_candidatos = {
        str(c.get("sku")).strip(): c
        for c in lista_candidatos
        if isinstance(c, dict) and c.get("sku")
    }

    if sku is not None:
        if sku not in mapa_candidatos:
            justificacion = (
                f"SKU '{sku}' rechazado por seguridad: no pertenece a los candidatos recuperados de DynamoDB "
                f"({list(mapa_candidatos.keys())}). {justificacion}"
            )
            sku = None
            confianza = 0.0
            requiere_revision = True
        else:
            # 6. Criterio 100% LLM: se omite el peso de la similitud coseno vectorial
            # para no penalizar matches correctos debido a diferencias de formato en glosas crudas.
            pass

    # 7. Coherencia final de revisión humana
    if sku is None or confianza < umbral_confianza:
        requiere_revision = True

    validado = ResultadoHomologacion(
        sku_seleccionado=sku,
        confianza=round(confianza, 4),
        justificacion=justificacion,
        requiere_revision=requiere_revision
    )
    return validado.model_dump()


# Template de Inferencia para Homologación
PROMPT_HOMOLOGACION = ChatPromptTemplate.from_messages([
    ("system", "Eres un asistente experto en inventarios de botillería y distribución de licores en Chile. Responde exclusivamente en formato JSON."),
    ("human", """Tu tarea es homologar una línea cruda de factura de proveedor con uno de los productos de nuestro catálogo interno.

LÍNEA DE FACTURA:
- Glosa original: "{glosa_factura}"
- Formato detectado: "{formato_factura}"

CANDIDATOS DEL CATÁLOGO RECUPERADOS DESDE DYNAMODB:
{candidatos}

INSTRUCCIONES:
1. Evalúa abreviaciones comunes de distribuidores chilenos (ej: "mst" = Mistral, "cerv cor" = Cerveza Corona, "7lts" o "750" = 750cc, "35" = 35°).
2. Si uno de los candidatos coincide claramente en marca, tipo de licor y formato/calibre, entrega ÚNICAMENTE el "sku" exacto de ese candidato en "sku_seleccionado" y un nivel de "confianza" entre 0.0 y 1.0.
3. No inventes SKUs que no estén en la lista de CANDIDATOS RECUPERADOS.
4. Si ninguno coincide o la descripción es muy ambigua/incompleta, define "sku_seleccionado" como null y marca "requiere_revision" como true.

Responde ÚNICAMENTE en formato JSON con la siguiente estructura:
{{
  "sku_seleccionado": "SKU o null",
  "confianza": 0.95,
  "justificacion": "Breve razón del match",
  "requiere_revision": false
}}
""")
])


@tool
def razonar_homologacion(
    glosa_factura: str,
    formato_factura: Optional[str] = None,
    candidatos: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    Utiliza el LLM para razonar sobre equivalencias y abreviaturas comerciales entre una glosa de factura
    y los candidatos recuperados desde DynamoDB. Valida tipos con Pydantic y calibra la confianza.
    
    Args:
        glosa_factura (str): Glosa original de la línea de factura.
        formato_factura (str | None): Formato o calibre detectado.
        candidatos (list[dict] | None): Candidatos recuperados con búsqueda vectorial nativa.
        
    Returns:
        dict: Diccionario validado con 'sku_seleccionado', 'confianza', 'justificacion' y 'requiere_revision'.
    """
    lista_candidatos = candidatos or []
    if not lista_candidatos:
        return validar_respuesta_homologacion({
            "sku_seleccionado": None,
            "confianza": 0.0,
            "justificacion": "No hay candidatos disponibles en DynamoDB para homologar.",
            "requiere_revision": True
        }, candidatos=[])

    logger.info("Razonando homologación con LLM para glosa: '%s' contra %d candidatos", glosa_factura, len(lista_candidatos))
    llm = get_reasoning_llm(temperature=0.0)
    cadena = PROMPT_HOMOLOGACION | llm | JsonOutputParser()

    resultado_crudo = cadena.invoke({
        "glosa_factura": glosa_factura,
        "formato_factura": formato_factura or "No especificado",
        "candidatos": json.dumps(lista_candidatos, indent=2, ensure_ascii=False)
    })

    return validar_respuesta_homologacion(resultado_crudo, candidatos=lista_candidatos)


@tool
def evaluar_variacion_costo(
    costo_factura: float,
    costo_actual: float,
    umbral_alerta: float = 0.15
) -> Dict[str, Any]:
    """
    Evalúa la variación porcentual entre el costo unitario de la factura y el costo registrado en DynamoDB.
    Si el aumento supera el umbral (por defecto 15%), genera una alerta para ajuste de precio de venta.
    
    Args:
        costo_factura (float): Costo unitario según la factura.
        costo_actual (float): Costo unitario actual en DynamoDB.
        umbral_alerta (float, optional): Umbral de incremento (ej. 0.15 para 15%). Default es 0.15.
        
    Returns:
        dict: Diccionario con bandera de alerta, porcentaje de aumento y recomendación.
    """
    if costo_actual <= 0:
        return {
            "alerta": False,
            "variacion_porcentual": 0.0,
            "mensaje": "No existe costo anterior registrado en el catálogo para comparar."
        }

    diferencia = costo_factura - costo_actual
    variacion = diferencia / costo_actual

    alerta = variacion > umbral_alerta
    mensaje = (
        f"Alerta de aumento de costo: Subió un {variacion * 100:.1f}% "
        f"(de ${costo_actual:,.0f} a ${costo_factura:,.0f}). Se sugiere evaluar aumento en precio de venta."
        if alerta else
        f"Variación de costo dentro del margen tolerado ({variacion * 100:.1f}%)."
    )

    return {
        "alerta": alerta,
        "variacion_porcentual": round(variacion * 100, 2),
        "diferencia_pesos": diferencia,
        "mensaje": mensaje
    }

