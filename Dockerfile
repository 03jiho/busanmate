FROM python:3.11-slim
WORKDIR /app
# PDF 내보내기용 한글 폰트 (ui/export.py)
RUN apt-get update && apt-get install -y --no-install-recommends fonts-nanum && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
