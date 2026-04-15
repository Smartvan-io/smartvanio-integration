ARG BUILD_FROM=ghcr.io/home-assistant/aarch64-base:3.21
FROM ${BUILD_FROM}

# Install runtime dependencies
RUN apk add --no-cache git python3 py3-yaml

# Copy all app files in one layer
COPY run.sh /
COPY configure_yaml.py /opt/smartvanio/configure_yaml.py
COPY dashboards/ /opt/smartvanio/dashboards/

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
