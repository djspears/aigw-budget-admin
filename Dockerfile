# Optional: a self-contained image of the app (the Kubernetes deployment in deploy/ does not need it; it mounts the
# app files from a ConfigMap into the stock python:3.12-slim image). Build and push to your registry, then set
# APP_IMAGE in config.env if you prefer an image.
FROM python:3.12-slim
COPY app/ /app/
USER 65532:65532
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LISTEN=127.0.0.1:8080
EXPOSE 8080
CMD ["python3", "/app/server.py"]
