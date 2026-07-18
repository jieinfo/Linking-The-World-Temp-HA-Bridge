ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /app
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY bridge.py addon_entrypoint.py ./
CMD ["python3", "/app/addon_entrypoint.py"]
