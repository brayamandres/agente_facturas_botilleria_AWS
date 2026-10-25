"""
Definición del Estado del Agente (AgentState) para LangGraph en el Worker.

Mantiene la memoria de trabajo de corta duración a lo largo de todos los nodos del grafo,
incluyendo trazabilidad de la factura recibida desde S3/SQS, ítems evaluados,
candidatos recuperados de DynamoDB y eventos de auditoría.
"""
from typing import TypedDict, Optional, List, Dict, Any


class AgentState(TypedDict):
    """Estado global del flujo de trabajo del agente de facturas."""
    # 1. Datos de entrada (provenientes del evento SQS / S3)
    ruta_imagen: str
    factura_id: Optional[str]
    s3_bucket: Optional[str]
    s3_key: Optional[str]

    # 2. Percepción y extracción multimodal (Gemini Vision)
    datos_factura: Dict[str, Any]

    # 3. Control adaptativo de duplicados
    es_duplicada: bool
    info_duplicada: Optional[Dict[str, Any]]

    # 4. Iteración y planificación de ítems
    items_pendientes: List[Dict[str, Any]]
    item_actual: Optional[Dict[str, Any]]
    candidatos_actuales: List[Dict[str, Any]]

    # 5. Razonamiento asistido por LLM y análisis de precios
    decision_actual: Optional[Dict[str, Any]]
    alerta_precio: Optional[Dict[str, Any]]

    # 6. Resultados acumulados y control de revisión
    items_procesados: List[Dict[str, Any]]
    requiere_intervencion: bool
    datos_intervencion_humana: Optional[Dict[str, Any]]

    # 7. Memoria de largo plazo y observabilidad
    auditoria_id: Optional[str]
    mensajes_bitacora: List[str]
    estado_final: str

