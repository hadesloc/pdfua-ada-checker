FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    default-jre-headless \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /opt/verapdf && \
    curl -fsSL https://repo1.maven.org/maven2/org/verapdf/apps/cli/1.30.3/cli-1.30.3.jar \
    -o /opt/verapdf/cli-1.30.3.jar && \
    chmod 0644 /opt/verapdf/cli-1.30.3.jar

RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PORT=7860 \
    VERAPDF_JAR=/opt/verapdf/cli-1.30.3.jar

WORKDIR /home/user/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

COPY --chown=user app.py .

EXPOSE 7860
CMD ["python", "app.py"]
