# Rehearsal in one container: the console, the demo store and its Synapse generated MCP server.
#   docker build -t rehearsal .
#   docker run --rm -p 8000:8000 rehearsal          then open http://localhost:8000
# Without a model key the console still runs every recorded run and the attack simulation.
# Add --env-file .env to run the live agent with your own free keys.

FROM node:20-slim AS console
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
# Run as an unprivileged user.
RUN useradd --create-home --uid 1000 user
WORKDIR /app
COPY requirements.txt ./
COPY legacy/kestrel_desk/requirements.txt legacy/kestrel_desk/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=user . .
COPY --from=console --chown=user /web/dist web/dist
USER user
ENV PYTHONUNBUFFERED=1 \
    REHEARSAL_HOST=0.0.0.0 \
    REHEARSAL_PORT=8000 \
    REHEARSAL_PUBLIC_DEMO=1
EXPOSE 8000
CMD ["python", "-m", "rehearsal.server"]
