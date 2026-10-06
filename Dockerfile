FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static
COPY data ./data

ENV PORT=8080
ENV POLL_SECONDS=900
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["python", "app/main.py"]
