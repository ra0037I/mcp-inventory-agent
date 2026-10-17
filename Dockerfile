# Containerised MCP server (streamable HTTP), analogous to lecture Sample 4
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py inventory_db.py discount_engine.py audit_log.py ./
EXPOSE 8000
CMD ["python", "server.py", "--http", "--host", "0.0.0.0", "--port", "8000"]
