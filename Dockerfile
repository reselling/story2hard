FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sync.py .

# /data is where state.json lives — mount a volume here for persistence
VOLUME ["/data"]

CMD ["python", "-u", "sync.py"]
