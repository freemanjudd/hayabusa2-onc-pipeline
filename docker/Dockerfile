# ISIS environment for the Hayabusa2 ONC-W2 pipeline.
#
# Built ONCE by .github/workflows/build-isis-image.yml and pushed to
# ghcr.io/<owner>/<repo>/isis. Rebuild only when THIS file changes — the
# process-images workflow just pulls the finished image.
#
# It deliberately does NOT bundle ISISDATA (the SPICE/calibration data area is
# huge). process-images downloads a Hayabusa2-only slice at run time and caches
# it. See CLAUDE.md.

FROM mambaorg/micromamba:2.0.5

LABEL org.opencontainers.image.description="USGS ISIS + rclone for Hayabusa2 ONC-W2 processing"
LABEL org.opencontainers.image.source="https://github.com/freemanjudd/hayabusa2-onc-pipeline"

USER root
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl bash coreutils \
 && rm -rf /var/lib/apt/lists/*
USER $MAMBA_USER

# ISIS from the USGS Astrogeology channel; rclone is what `downloadIsisData` uses.
RUN micromamba install -y -n base \
        -c usgs-astrogeology -c conda-forge \
        isis rclone \
 && micromamba clean --all --yes

# conda env prefix that micromamba installs into
ENV ISISROOT=/opt/conda
# where the process-images job will download the Hayabusa2 data slice
# (the `docker run -e ISISDATA=/isisdata` in process-images.yml also sets this)
ENV ISISDATA=/isisdata
ENV PATH=/opt/conda/bin:$PATH

# Fail the build early if ISIS didn't land properly. `micromamba run -n base`
# executes inside the activated env without relying on the entrypoint.
RUN micromamba run -n base bash -c '\
      set -eu; \
      (isisversion || echo "note: isisversion not available"); \
      for c in hyb2onc2isis hyb2onccal spiceinit isis2std downloadIsisData rclone; do \
        p="$(command -v "$c")" && echo "ok: $p" || { echo "MISSING: $c"; exit 1; }; \
      done'

# _entrypoint.sh activates the base env, then execs the given command:
#   docker run <image> bash scripts/isis_pipeline.sh data/raw web/data/images
ENTRYPOINT ["/usr/local/bin/_entrypoint.sh"]
CMD ["bash"]
