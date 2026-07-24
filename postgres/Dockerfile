FROM postgres:16-alpine

COPY entrypoint.sh /usr/local/bin/kubefleet-pg-entrypoint.sh
RUN chmod +x /usr/local/bin/kubefleet-pg-entrypoint.sh

ENV PGDATA=/var/lib/postgresql/data/pgdata
ENV POSTGRES_ROLE=primary
ENV POSTGRES_PASSWORD=postgres
ENV REPLICATION_USER=replicator
ENV REPLICATION_PASSWORD=replpass

USER postgres
ENTRYPOINT ["kubefleet-pg-entrypoint.sh"]
