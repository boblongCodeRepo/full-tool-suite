# Portfolio Checkup - small, stateless container. Visitors' files are processed in their browsers; the server
# only serves static files and cached public economic data, and stores nothing.
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py .
COPY static ./static
ENV HOST=0.0.0.0 PORT=8080
EXPOSE 8080
RUN useradd --create-home appuser
USER appuser
CMD ["python", "server.py"]
