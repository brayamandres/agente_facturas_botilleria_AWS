"""
Configuración del Microservicio de API (Ingesta Web).
Gestiona variables de entorno y clientes de AWS S3 y SQS sin credenciales embebidas.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
import boto3

# Cargar variables de entorno desde .env (en api/ o en la raíz del proyecto)
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
if (BASE_DIR / ".env").exists():
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)
elif (ROOT_DIR / ".env").exists():
    load_dotenv(dotenv_path=ROOT_DIR / ".env", override=True)

# Parámetros de Región y Configuración AWS
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN")
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL")  # Para pruebas locales con LocalStack

# Nombres de Recursos AWS
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "botilleria-facturas-raw")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
DYNAMODB_TABLE_PRODUCTOS = os.getenv("DYNAMODB_TABLE_PRODUCTOS", "botilleria-productos")
DYNAMODB_TABLE_HISTORIAL = os.getenv("DYNAMODB_TABLE_HISTORIAL", "botilleria-historial-facturas")


def _get_boto3_kwargs():
    """Genera argumentos comunes para clientes boto3 respetando IAM Roles o variables."""
    kwargs = {"region_name": AWS_REGION}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
        if AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = AWS_SESSION_TOKEN
    return kwargs


def get_s3_client():
    """Retorna el cliente boto3 para Amazon S3."""
    return boto3.client("s3", **_get_boto3_kwargs())


def get_sqs_client():
    """Retorna el cliente boto3 para Amazon SQS."""
    return boto3.client("sqs", **_get_boto3_kwargs())


def get_dynamodb_resource():
    """Retorna el recurso boto3 de alto nivel para operaciones CRUD en DynamoDB."""
    return boto3.resource("dynamodb", **_get_boto3_kwargs())


def get_dynamodb_client():
    """Retorna el cliente boto3 de bajo nivel para DynamoDB."""
    return boto3.client("dynamodb", **_get_boto3_kwargs())
