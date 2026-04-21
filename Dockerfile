FROM python:3.10.11-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ ./src/
COPY public/ ./public/

RUN pip install --no-cache-dir .

EXPOSE 8080

# USER_INTERFACE: BOT_SERVICE (default) or CHAINLIT_UI
ENV USER_INTERFACE=BOT_SERVICE
ENV PORT=8080
ENV WORKERS=1

CMD ["python", "-m", "m365_langchain_agent"]
