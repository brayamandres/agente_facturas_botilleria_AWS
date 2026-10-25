# ==============================================================================
# Dockerfile: Microservicio de Ingesta Web (API)
# Base ligera optimizada con python:3.11-slim para bajo consumo de memoria (~120MB)
# ==============================================================================
FROM python:3.11-slim

# Evitar escritura de archivos .pyc y forzar volcado de logs inmediato para CloudWatch
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

WORKDIR /app

# Instalar dependencias del sistema mínimas requeridas
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copiar e instalar dependencias de Python en una capa dedicada para optimizar caché
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --upgrade pip && \
    pip install -r requirements.txt

# Copiar el código del microservicio
COPY . /app/api

# Exponer el puerto del Application Load Balancer
EXPOSE 8000

# Sonda de salud (Health Check) local para Docker y ECS
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Comando de inicio del servidor ASGI Uvicorn
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
