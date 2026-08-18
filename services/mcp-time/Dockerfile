FROM python:3.12-slim
RUN pip install --no-cache-dir mcp-server-time mcp-proxy
EXPOSE 8000
CMD ["mcp-proxy", "--transport", "streamablehttp", "--host", "0.0.0.0", "--port", "8000", "--", "mcp-server-time"]
