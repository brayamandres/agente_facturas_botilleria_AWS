"""
Configuración del Microservicio Worker (LangGraph + IA).
Gestiona variables de entorno, modelos de lenguaje, embeddings y clientes de AWS.
"""
import os
from pathlib import Path
from dotenv import load_dotenv
import boto3

# Directorios de referencia
BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
if (BASE_DIR / ".env").exists():
    load_dotenv(dotenv_path=BASE_DIR / ".env", override=True)
elif (ROOT_DIR / ".env").exists():
    load_dotenv(dotenv_path=ROOT_DIR / ".env", override=True)

DATA_DIR = ROOT_DIR / "data"
DATA_OPTIMIZADO_DIR = DATA_DIR / "optimizado"
DATA_OPTIMIZADO_DIR.mkdir(parents=True, exist_ok=True)

# 1. Configuración AWS (Credenciales y Endpoints)
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")
AWS_SESSION_TOKEN = os.getenv("AWS_SESSION_TOKEN")
AWS_ENDPOINT_URL = os.getenv("AWS_ENDPOINT_URL")

# Recursos AWS
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "botilleria-facturas-raw")
SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL", "")
DYNAMODB_TABLE_PRODUCTOS = os.getenv("DYNAMODB_TABLE_PRODUCTOS", "botilleria-productos")
DYNAMODB_TABLE_HISTORIAL = os.getenv("DYNAMODB_TABLE_HISTORIAL", "botilleria-historial-facturas")
DYNAMODB_VECTOR_INDEX_NAME = os.getenv("DYNAMODB_VECTOR_INDEX_NAME", "ProductosVectorIndex")

# 2. Configuración de Modelos de IA
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_GEMINI_MODELS = [
    "gemini-2.5-flash",
    "gemini-2.0-flash",
    "gemini-3.5-flash",
    "gemini-3.8-flash",
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro"
]

# 3. Configuración de Embeddings Vectoriales (384d)
EMBEDDING_MODEL_NAME = "paraphrase-multilingual-MiniLM-L12-v2"
EMBEDDING_DIMENSIONS = 384

# 4. Observabilidad con LangSmith (Opcional)
LANGSMITH_API_KEY = os.getenv("LANGSMITH_API_KEY") or os.getenv("LANGCHAIN_API_KEY")
if LANGSMITH_API_KEY and not LANGSMITH_API_KEY.startswith("YOUR_"):
    os.environ["LANGCHAIN_TRACING_V2"] = "true"
    os.environ["LANGCHAIN_ENDPOINT"] = "https://api.smith.langchain.com"
    os.environ["LANGCHAIN_API_KEY"] = LANGSMITH_API_KEY
    os.environ["LANGCHAIN_PROJECT"] = os.getenv("LANGCHAIN_PROJECT", "agente-facturas-botilleria-aws")
else:
    os.environ["LANGCHAIN_TRACING_V2"] = "false"


def _get_boto3_kwargs():
    """Genera los argumentos para clientes boto3 respetando IAM Roles o variables."""
    kwargs = {"region_name": AWS_REGION}
    if AWS_ENDPOINT_URL:
        kwargs["endpoint_url"] = AWS_ENDPOINT_URL
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = AWS_SECRET_ACCESS_KEY
        if AWS_SESSION_TOKEN:
            kwargs["aws_session_token"] = AWS_SESSION_TOKEN
    return kwargs


def get_dynamodb_client():
    """Retorna el cliente boto3 de bajo nivel para DynamoDB (usado para search_vectors)."""
    return boto3.client("dynamodb", **_get_boto3_kwargs())


def get_dynamodb_resource():
    """Retorna el recurso boto3 de alto nivel para operaciones CRUD en DynamoDB."""
    return boto3.resource("dynamodb", **_get_boto3_kwargs())


def get_s3_client():
    """Retorna el cliente boto3 para Amazon S3."""
    return boto3.client("s3", **_get_boto3_kwargs())


def get_sqs_client():
    """Retorna el cliente boto3 para Amazon SQS."""
    return boto3.client("sqs", **_get_boto3_kwargs())

