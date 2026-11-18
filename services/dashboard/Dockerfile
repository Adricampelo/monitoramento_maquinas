FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY interface.py .

RUN useradd --uid 10001 --no-create-home app
USER 10001

EXPOSE 8501
CMD ["streamlit", "run", "interface.py", \
     "--server.port=8501", "--server.address=0.0.0.0"]
