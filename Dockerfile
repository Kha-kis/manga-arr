FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends unrar-free curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir fastapi uvicorn[standard] httpx jinja2 python-multipart rarfile defusedxml
# Self-host HTMX + Alpine so the app has no CDN runtime dependency
RUN mkdir -p /app/static \
    && curl -sLo /app/static/htmx.min.js \
       https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js \
    && curl -sLo /app/static/alpine.min.js \
       https://unpkg.com/alpinejs@3.14.3/dist/cdn.min.js
COPY app/ /app/
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
