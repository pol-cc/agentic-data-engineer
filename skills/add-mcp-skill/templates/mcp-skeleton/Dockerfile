FROM python:3.12-slim

# git is required at RUNTIME — the write tools shell out to it. The client repo
# clone is mounted at /repo (read-write) by docker-compose, not baked in.
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./

EXPOSE 8000
CMD ["python", "server.py"]
