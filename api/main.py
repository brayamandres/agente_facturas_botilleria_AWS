"""
Microservicio API de Ingesta Web para Facturas de Botillería.

Arquitectura:
- Patrón Microservicios: Desacoplado de la lógica de IA (sin LangGraph ni Gemini).
- Patrón Orientado a Eventos (Event-Driven): Publicación asíncrona en AWS SQS y AWS S3.
- Observabilidad: Logging estructurado apto para Amazon CloudWatch.
- Resiliencia: Health checks para Application Load Balancer (ALB).
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from fastapi import FastAPI, UploadFile, File, HTTPException, status
from fastapi.responses import JSONResponse

from api.config import (
    S3_BUCKET_NAME,
    SQS_QUEUE_URL,
    get_s3_client,
    get_sqs_client
)

# Configuración de Logging Estructurado para CloudWatch
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [API] %(name)s - %(message)s"
)
logger = logging.getLogger("api.main")

app = FastAPI(
    title="API Ingesta de Facturas - Botillería AWS",
    description="Microservicio liviano para recepción de facturas, persistencia en S3 y encolamiento en SQS",
    version="2.0.0"
)


@app.get("/health", status_code=status.HTTP_200_OK, summary="Health Check para AWS ALB")
def health_check():
    """
    Sonda de salud requerida por el Application Load Balancer (ALB) y AWS ECS.
    Retorna 200 OK indicando que el contenedor está activo y saludable.
    """
    return {"status": "ok", "service": "api-ingesta", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post(
    "/facturas/upload",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Recepción asíncrona de facturas"
)
async def upload_factura(file: UploadFile = File(...)):
    """
    Recibe una imagen de factura (PNG, JPG, JPEG), la almacena en Amazon S3
    y publica un evento en la cola AWS SQS para procesamiento asíncrono.
    
    Retorna HTTP 202 Accepted inmediatamente (desacoplamiento temporal).
    """
    if not file.filename:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nombre de archivo inválido o ausente."
        )

    # Generación de identificador único de correlación (Traceability)
    factura_id = str(uuid.uuid4())
    logger.info("Factura recibida. ID generado: %s | Archivo original: %s", factura_id, file.filename)

    extension = file.filename.split(".")[-1].lower() if "." in file.filename else "png"
    s3_key = f"raw/{factura_id}.{extension}"

    try:
        # 1. Leer bytes de la imagen
        contenido = await file.read()
        if len(contenido) == 0:
            logger.warning("Factura ID %s rechazada: archivo vacío", factura_id)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="El archivo proporcionado está vacío."
            )

        # 2. Persistir en Amazon S3
        s3 = get_s3_client()
        logger.info("Subiendo imagen a S3 bucket: '%s', key: '%s'", S3_BUCKET_NAME, s3_key)
        s3.put_object(
            Bucket=S3_BUCKET_NAME,
            Key=s3_key,
            Body=contenido,
            ContentType=file.content_type or "image/png",
            Metadata={
                "factura_id": factura_id,
                "nombre_original": file.filename,
                "fecha_recepcion": datetime.now(timezone.utc).isoformat()
            }
        )

        # 3. Publicar evento en AWS SQS
        evento = {
            "factura_id": factura_id,
            "s3_bucket": S3_BUCKET_NAME,
            "s3_key": s3_key,
            "nombre_original": file.filename,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }

        if SQS_QUEUE_URL:
            sqs = get_sqs_client()
            logger.info("Encolando evento en SQS para Factura ID: %s", factura_id)
            sqs.send_message(
                QueueUrl=SQS_QUEUE_URL,
                MessageBody=json.dumps(evento)
            )
        else:
            logger.warning("SQS_QUEUE_URL no configurada. Evento persistido solo en S3 para Factura ID: %s", factura_id)

        # 4. Respuesta HTTP 202 Accepted (Patrón Asíncrono / Event-Driven)
        return JSONResponse(
            status_code=status.HTTP_202_ACCEPTED,
            content={
                "factura_id": factura_id,
                "status": "ACEPTADO",
                "mensaje": "Factura recibida y encolada exitosamente para procesamiento asíncrono.",
                "s3_key": s3_key,
                "timestamp": evento["timestamp"]
            }
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error("Error crítico durante la ingesta de la factura ID %s: %s", factura_id, str(e), exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Fallo interno en el almacenamiento o encolamiento de la factura: {str(e)}"
        )

