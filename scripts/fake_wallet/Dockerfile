# Wallet falsa: sólo para desarrollo local (perfil "fake" de docker-compose.yml).
FROM python:3.12-slim

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY fake_wallet ./fake_wallet

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health')"
CMD ["uvicorn", "fake_wallet.main:app", "--host", "0.0.0.0", "--port", "8080"]
