"""
Servicio de Embeddings Vectoriales usando SentenceTransformers.
Genera vectores normalizados de 384 dimensiones para la búsqueda semántica en DynamoDB.
"""
from sentence_transformers import SentenceTransformer
from worker.config import EMBEDDING_MODEL_NAME

_embedding_model = None

def get_embedding_model():
    """
    Retorna la instancia del modelo de SentenceTransformers (Singleton).
    Carga el modelo en memoria únicamente la primera vez que se solicita.
    """
    global _embedding_model
    if _embedding_model is None:
        _embedding_model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedding_model


def generar_embedding(texto: str) -> list[float]:
    """
    Genera un vector de embedding (384 dimensiones) para el texto proporcionado.
    
    Args:
        texto (str): Texto para el cual se generará el embedding.
    
    Returns:
        list[float]: Vector de embedding como lista de floats.
    """
    if not texto or not texto.strip():
        return [0.0] * 384
    model = get_embedding_model()
    vector = model.encode(texto.strip())
    return vector.tolist()

