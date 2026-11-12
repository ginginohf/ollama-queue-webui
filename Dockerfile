FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py index.html ./
RUN useradd -m app && mkdir /data && chown app:app /data
USER app
ENV AI_DATA_DIR=/data
EXPOSE 5000
CMD ["gunicorn","--workers","1","--threads","8","--bind","0.0.0.0:5000","--timeout","120","server:app"]
