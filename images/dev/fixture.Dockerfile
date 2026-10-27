# syntax=docker/dockerfile:1.28.0@sha256:bb22d9815c728170f72750f4e5b0d672e06176142e1d602c7e66c050100b7e5b
# Dev image for the docker tests' fixture repo (tests/jobs/test_coord_loop_docker.py).
# Not used for any real repo. The fixture's checks are pytest tests (the
# verifier needs a test report), so the image is CPython 3.14.8 with pytest,
# hash-locked (the same pins as vigil's own uv.lock, released by 2026-10-08).

FROM python:3.14.8-slim-trixie@sha256:fbecd461fc72d21b2143c6eaba25143738079154a6d0ea3da3aec724f50eed4a

COPY <<'EOF' /tmp/requirements.txt
iniconfig==2.3.1 \
    --hash=sha256:67f4b9c50da0dedf52af349e7749a80a9057a5031199791b906c3bb3ae878960 \
    --hash=sha256:9121e2c1fdb355232495be3194c8dfe87ccc2d5dee45947b78e68f499790d7a7
packaging==26.3 \
    --hash=sha256:94edc256424af38762eb31306eed28beb9f0efc50a8837492c9d6fd6004aed79 \
    --hash=sha256:d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c
pluggy==1.6.0 \
    --hash=sha256:7dcc130b76258d33b90f61b658791dede3486c3e6bfb003ee5c9bfb396dd22f3 \
    --hash=sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746
pygments==2.21.0 \
    --hash=sha256:2363c69b61c4a97c838da3b130dcd6468f4848992b21a82f2a63ec34377137d9 \
    --hash=sha256:610ca751c9bc2492b38eb9a38a7fbc93edbbb2d7182edaf34e66ae493dee5c8c
pytest==9.1.1 \
    --hash=sha256:1088fbde8f2b49d95a549a195707afa7a76a3ce9bcadc26b6d71f0ffda5fe313 \
    --hash=sha256:37a86b45efb9a47a61a36449063e8e18d0cab3161329fc099eb21783169c4f0c
EOF
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt && rm /tmp/requirements.txt \
 && (id -u 10001 >/dev/null 2>&1 || useradd -u 10001 -U -M -d /tmp/home -s /bin/sh vigil)
USER 10001:10001
WORKDIR /work
