# ==============================================================================
# Dockerfile: Microservicio de Procesamiento Pesado (Worker)
# Base optimizada con python:3.11-slim para inferencia de IA y LangGraph
# ==============================================================================
FROM python:3.11-slim

# Evitar escritura de archivos .pyc y habilitar salida de logs inmediata a CloudWatch
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Instalar librerías mínimas del sistema requeridas para Pillow y dependencias C
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Instalar PyTorch CPU-only previamente para evitar descargar CUDA y paquetes nvidia-* (~2.5 GB)
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install torch==2.6.0+cpu --index-url https://download.pytorch.org/whl/cpu

# Copiar e instalar dependencias en una capa aislada para aprovechar la caché de Docker
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Copiar el código fuente del microservicio worker
COPY . /app/worker

# Crear directorio temporal para el procesamiento efímero stateless
RUN mkdir -p /tmp

# Comando de inicio del consumidor asíncrono SQS
CMD ["python", "-m", "worker.main"]

