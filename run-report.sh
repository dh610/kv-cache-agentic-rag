#!/bin/sh
# macOS/Linux/WSL entry point. Installs only into this project and uv's user cache.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
mode=live
case "${1:-}" in
  '') ;;
  --mock) mode=mock ;;
  --prepare-only) mode=prepare ;;
  --help|-h)
    printf '%s\n' 'Usage: ./run-report.sh [--mock | --prepare-only]' \
      'Default: prepare dependencies, papers and embeddings, then generate a live report.' \
      'Set OPENAI_API_KEY and TAVILY_API_KEY in .env or exported environment variables.' \
      '--prepare-only: prepare local resources without calling paid APIs.' \
      '--mock: generate a wiring-test report without keys, papers or model downloads.'
    exit 0 ;;
  *) printf '%s\n' 'Unknown option. Use ./run-report.sh --help' >&2; exit 1 ;;
esac
[ "$#" -le 1 ] || { printf '%s\n' 'Use one option only.' >&2; exit 1; }
if command -v uv >/dev/null 2>&1; then
  uv_bin=$(command -v uv)
elif [ -x "$PWD/.cache/tools/uv" ]; then
  uv_bin="$PWD/.cache/tools/uv"
else
  command -v curl >/dev/null 2>&1 || { printf '%s\n' 'Install curl or uv, then retry.' >&2; exit 1; }
  printf '%s\n' '[1/4] Installing uv 0.12.1 into .cache/tools (shell profiles unchanged)'
  mkdir -p .cache/tools
  curl --proto '=https' --tlsv1.2 -fsSL --retry 2 https://astral.sh/uv/0.12.1/install.sh -o .cache/tools/install-uv.sh
  UV_UNMANAGED_INSTALL="$PWD/.cache/tools" sh .cache/tools/install-uv.sh
  uv_bin="$PWD/.cache/tools/uv"
fi
# Safe defaults for BGE on macOS CPU; explicit caller settings take precedence.
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"
printf '%s\n' '[1/4] Preparing Python 3.11 and locked dependencies'
"$uv_bin" sync --frozen --python 3.11 --inexact
if [ "$mode" = mock ]; then
  exec "$uv_bin" run --no-sync python -m app.run_report --mock
fi
if [ "$mode" = live ]; then
  "$uv_bin" run --no-sync python -m app.run_report --check-keys
fi
"$uv_bin" sync --frozen --python 3.11 --extra rag
if [ "$mode" = prepare ]; then
  exec "$uv_bin" run --no-sync python -m app.run_report --prepare-only
fi
exec "$uv_bin" run --no-sync python -m app.run_report
