# syntax=docker/dockerfile:1
# smokescreen egress proxy, one sidecar per session (B4, B5). linux/arm64.
# Built locally from the release binary (ghcr pulls were denied), checked
# against the release's published SHA-256.
FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a AS fetch
ARG SMOKESCREEN_VERSION=v0.1.0
ARG SMOKESCREEN_SHA256=f15ddd0dc2b2cbc6d7318f441928939eb9bedfab2a1881508af3096a1e24b7c9
ADD --checksum=sha256:${SMOKESCREEN_SHA256} \
    https://github.com/stripe/smokescreen/releases/download/${SMOKESCREEN_VERSION}/smokescreen_${SMOKESCREEN_VERSION}_linux_arm64.tar.gz \
    /tmp/smokescreen.tar.gz
RUN mkdir /out && tar -xzf /tmp/smokescreen.tar.gz -C /out --strip-components=1

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a
COPY --from=fetch /out/smokescreen /usr/local/bin/smokescreen
RUN groupadd -g 10002 smokescreen && useradd -u 10002 -g 10002 -M -s /usr/sbin/nologin smokescreen
USER 10002:10002
EXPOSE 4750
# The ACL is mounted read-only at /etc/smokescreen/acl.yaml by vigild.
ENTRYPOINT ["/usr/local/bin/smokescreen", "--listen-ip=0.0.0.0", "--listen-port=4750", \
            "--egress-acl-file=/etc/smokescreen/acl.yaml"]
