FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY encaminhador.py .

RUN useradd --uid 10001 --no-create-home app
USER 10001

EXPOSE 8001
CMD ["python", "encaminhador.py"]
