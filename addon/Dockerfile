ARG BUILD_FROM
FROM ${BUILD_FROM}

# Install runtime dependencies
RUN apk add --no-cache bash curl jq

# Copy add-on files
COPY run.sh /
COPY integration/ /opt/smartvanio/integration/
COPY cards/ /opt/smartvanio/cards/
COPY dashboards/ /opt/smartvanio/dashboards/

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
