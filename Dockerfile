FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt pyproject.toml gsc_server.py ./

RUN pip install --no-cache-dir -r requirements.txt && pip install --no-cache-dir -e .

ENV GSC_SKIP_OAUTH=true
ENV SEO_AUDIT_ENABLE_WRITE_TOOLS=false
ENV SEO_AUDIT_ALLOW_PRIVATE_URLS=false
ENV LIGHTHOUSE_NO_SANDBOX=true

ENTRYPOINT ["mcp-seo-audit"]
