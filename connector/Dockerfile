FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Las dependencias se copian antes que el código para reutilizar la caché de capas
# cuando solo cambia el código.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./

RUN useradd --create-home --shell /usr/sbin/nologin energyshark
USER energyshark

# connector no expone HTTP, así que su chequeo es file-based: heartbeat.py sale 0
# solo si la señal de vida es reciente (RNF7).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python heartbeat.py || exit 1

CMD ["python", "consumer.py"]
