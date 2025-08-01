FROM python:3.11-slim

WORKDIR /app
COPY . .

RUN pip install --no-cache-dir -r requirements.txt

CMD ["gunicorn", "api:app", "-k", "gevent", "-w", "4", "--timeout", "120"]
