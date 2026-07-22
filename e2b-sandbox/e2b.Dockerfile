# E2B sandbox template: chdb on top of the official code-interpreter image.
#
# Build (needs the E2B CLI, an npm package — `npm i -g @e2b/cli`, then `e2b auth login`):
#   e2b template create chdb --dockerfile e2b.Dockerfile \
#     -c "sudo --preserve-env=E2B_LOCAL /root/.jupyter/start-up.sh" \
#     --ready-cmd "curl -sf http://localhost:49999/health"
#
# The -c/--ready-cmd flags are REQUIRED: `run_code` talks to a Jupyter-backed
# server on port 49999 that the base image ships but does NOT auto-start in a
# derived template — a template config's start command is not inherited from
# the base image's template. Without them every run_code call fails with
# "sandbox is running but port is not open" (port 49999, code 502).
# (`e2b template build` is the deprecated v1 command: it prints a banner and
# builds nothing.)
#
# E2B only supports Debian-based base images, which matches chdb's
# glibc-only wheel distribution. Do not switch to Alpine.

FROM e2bdev/code-interpreter:latest

RUN pip install --no-cache-dir chdb

# Bake the first engine load so a fresh sandbox answers its first query fast.
RUN python -c "import chdb; chdb.query('SELECT 1')"
