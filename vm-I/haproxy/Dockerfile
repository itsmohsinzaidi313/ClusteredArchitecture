FROM alpine
RUN apk add --no-cache haproxy curl
RUN adduser --disabled-password --home /app --gecos '' appuser && chown -R appuser /app
USER appuser
COPY haproxy.cfg /usr/local/etc/haproxy/haproxy.cfg
ENTRYPOINT ["haproxy", "-f", "/usr/local/etc/haproxy/haproxy.cfg"]