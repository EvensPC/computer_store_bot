FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY run.py ./
COPY app ./app
RUN mkdir -p /app/data /app/secrets
CMD ["python", "run.py"]
