ARG BASE_IMAGE
FROM ${BASE_IMAGE}
ARG RIPGREP_VERSION
ARG AST_GREP_VERSION
COPY project-search-setup.sh /tmp/project-search-setup.sh
RUN bash /tmp/project-search-setup.sh "$RIPGREP_VERSION" "$AST_GREP_VERSION" && rm /tmp/project-search-setup.sh
