FROM python:3.12-alpine
WORKDIR /app
COPY app.py engine.py index.html /app/
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "/app/app.py"]
