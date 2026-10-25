ARG BASE_IMAGE=docker.io/searxng/searxng:latest
FROM ${BASE_IMAGE}
COPY --chmod=0755 searxng/ /etc/searxng/
