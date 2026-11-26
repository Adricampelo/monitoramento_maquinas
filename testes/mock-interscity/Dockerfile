FROM python:3.12-slim
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0
WORKDIR /app
COPY mock.py .
USER 10001
CMD ["uvicorn", "mock:app", "--host", "0.0.0.0", "--port", "8000", "--log-level", "warning"]
