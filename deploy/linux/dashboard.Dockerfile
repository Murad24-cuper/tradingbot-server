FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tradingbot
# Same package versions as the Windows install (MetaTrader5 is skipped on Linux by its platform marker).
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY deploy/linux/panel-requirements.txt /tmp/panel-requirements.txt
RUN pip install --no-cache-dir -r /tmp/panel-requirements.txt
WORKDIR /opt/tradingbot
