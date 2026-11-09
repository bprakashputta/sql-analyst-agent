# syntax=docker/dockerfile:1.7
# Postgres 17 with the Chinook sample database (MIT, (c) Luis Rocha) and a read-only role for the app.
FROM postgres:17-alpine
ADD --checksum=sha256:e3fde5c1a5b51a2a91429a702c9ca6e69ba56e6c7f5e112724d70c3d03db695e \
    https://raw.githubusercontent.com/lerocha/chinook-database/7f67772503d71ba90f19283c38e93923addb43fa/ChinookDatabase/DataSources/Chinook_PostgreSql.sql \
    /docker-entrypoint-initdb.d/01-chinook.sql
COPY 10-readonly-role.sh /docker-entrypoint-initdb.d/10-readonly-role.sh
RUN chmod 0644 /docker-entrypoint-initdb.d/01-chinook.sql
