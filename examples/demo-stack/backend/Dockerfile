FROM python:3.13-slim
RUN pip install --no-cache-dir "psycopg[binary]==3.2.*"
WORKDIR /app
COPY app.py .
EXPOSE 8000
CMD ["python", "-u", "app.py"]
