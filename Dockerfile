FROM python:3.10.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY m365_langchain_agent/ ./m365_langchain_agent/

EXPOSE 8080

# USER_INTERFACE: BOT_SERVICE (default) or CHAINLIT_UI
ENV USER_INTERFACE=BOT_SERVICE
# DEPLOY_TARGET: CONTAINER_APPS (default) or KUBERNETES
ENV DEPLOY_TARGET=CONTAINER_APPS
ENV PORT=8080

CMD ["python", "app.py"]
