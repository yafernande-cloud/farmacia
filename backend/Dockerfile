# Imagen del backend (FastAPI) para la entrega académica.
#
# El contexto de compilación es la raíz del repositorio, porque `.python-version` y
# `requirements.txt` viven allí (Heroku también los lee desde la raíz) mientras que el código está
# en `backend/`. Construir desde `proyecto-farmacia/`:
#
#   docker build -f backend/Dockerfile -t cotizador-farmacia-backend .
#
# La publicación en producción sigue siendo **Manual Deploy en Heroku** por la responsable del
# proyecto; esta imagen no se publica en ningún registro. La configuración y los secretos se pasan
# al ejecutar el contenedor (nunca se copian dentro de la imagen; ver `.dockerignore`).

# Debe coincidir con `.python-version` (3.14). Se deja como ARG para poder fijarla sin editar todo.
ARG PYTHON_VERSION=3.14
FROM python:${PYTHON_VERSION}-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # El valor por defecto en `app/core/config.py` es relativo a la raíz del repositorio; en la
    # imagen se apunta a un directorio propio del usuario sin privilegios.
    STORAGE_DIR=/app/.storage \
    PORT=8000

WORKDIR /app

# Dependencias primero: la capa se reutiliza hasta que cambie `requirements.txt`.
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt && rm /tmp/requirements.txt

# Código del backend (app, migraciones, alembic.ini y fixtures). Sin pruebas, credenciales ni
# archivos locales: eso lo excluye `.dockerignore` en la raíz del contexto.
COPY backend/ /app/
# Se conserva la versión de Python declarada, para que la imagen sea trazable.
COPY .python-version /app/.python-version

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
    && mkdir -p /app/.storage \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Liveness: no toca la base de datos. Readiness (`/api/v1/health/ready`) sí, y se puede consultar
# aparte; el HEALTHCHECK usa liveness para no reiniciar la app por una base lenta.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,urllib.request; urllib.request.urlopen(f\"http://127.0.0.1:{os.environ.get('PORT','8000')}/api/v1/health\", timeout=3)"]

# Mismo arranque que el proceso `web` del Procfile de Heroku.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
