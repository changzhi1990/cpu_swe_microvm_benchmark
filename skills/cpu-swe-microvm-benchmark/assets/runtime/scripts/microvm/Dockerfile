ARG UBUNTU_VERSION=26.10
FROM ubuntu:${UBUNTU_VERSION}
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-pytest python3-numpy python3-pydantic python3-requests python3-jinja2 python3-rich python3-dotenv python3-platformdirs python3-yaml iproute2 bash coreutils git findutils procps mount util-linux diffutils ca-certificates && rm -rf /var/lib/apt/lists/*
