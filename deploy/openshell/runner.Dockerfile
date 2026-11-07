# Sandbox image for the IncidentLens runner. The runner uses only the Python standard
# library, so nothing is installed at run time and the sandbox needs no package egress.
# The base is pinned by digest; versions.json records the digest of the built image.
FROM nvcr.io/nvidia/base/ubuntu@sha256:d9461f428eea54e553add3a5158a0f245c6e1ea73efc59fff8e98e3d4a38e378

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
