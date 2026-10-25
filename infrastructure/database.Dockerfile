FROM pgvector/pgvector:pg17

# Package init inputs instead of bind-mounting 0600 host files into a different UID.
# Explicit image modes also avoid SELinux bind-mount labeling problems on Podman hosts.
COPY --chmod=0644 database/schema.sql /docker-entrypoint-initdb.d/01_schema.sql
COPY --chmod=0644 database/import.sql /docker-entrypoint-initdb.d/02_import.sql
COPY --chmod=0644 database/application.sql /docker-entrypoint-initdb.d/03_application.sql
COPY --chmod=0644 database/retrieval_indexes.sql /docker-entrypoint-initdb.d/04_retrieval_indexes.sql
COPY --chmod=0644 database/df_Uid_data_hack.csv /csv/df_Uid_data_hack.csv
COPY --chmod=0644 database/df_Uid_payment_hack.csv /csv/df_Uid_payment_hack.csv
COPY --chmod=0644 database/df_Fid_Uid_link_hack.csv /csv/df_Fid_Uid_link_hack.csv
COPY --chmod=0644 infrastructure/init-empty-database.sh /usr/local/bin/init-empty-database
RUN chmod 0755 /csv /docker-entrypoint-initdb.d
