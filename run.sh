#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHOICE_FILE="${ROOT_DIR}/.install_manager"
CONDA_ENV_FILE="${ROOT_DIR}/.install_conda_env"
DEFAULT_CONDA_ENV="ktxgo-env"
MANAGER=""

usage() {
  cat <<'EOF'
Usage: ./run.sh [options]

Options:
  -h, --help     Show this help
EOF
}

log() {
  printf '[run] %s\n' "$*"
}

fail() {
  printf '[run][error] %s\n' "$*" >&2
  exit 1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

load_manager() {
  if [[ ! -f "${CHOICE_FILE}" ]]; then
    fail "No setup choice found. Run ./install.sh first."
  fi

  MANAGER="$(tr -d '[:space:]' < "${CHOICE_FILE}")"
  case "${MANAGER}" in
    uv|conda) ;;
    *)
      fail "Unknown manager in ${CHOICE_FILE}: ${MANAGER}"
      ;;
  esac
  log "Using package manager: ${MANAGER}"
}

activate_uv() {
  local venv_activate="${ROOT_DIR}/.venv/bin/activate"
  [[ -f "${venv_activate}" ]] || fail "uv environment not found. Run ./install.sh --uv first."
  # shellcheck disable=SC1090
  source "${venv_activate}"
}

resolve_conda_env_name() {
  local env_name="${DEFAULT_CONDA_ENV}"
  if [[ -f "${CONDA_ENV_FILE}" ]]; then
    env_name="$(tr -d '[:space:]' < "${CONDA_ENV_FILE}")"
  fi
  if [[ -n "${CONDA_ENV_NAME:-}" ]]; then
    env_name="${CONDA_ENV_NAME}"
  fi
  printf '%s' "${env_name}"
}

activate_conda() {
  has_cmd conda || fail "conda command not found. Install/initialize conda first."

  local conda_base
  conda_base="$(conda info --base 2>/dev/null)" || fail "Failed to resolve conda base path."
  local conda_sh="${conda_base}/etc/profile.d/conda.sh"
  [[ -f "${conda_sh}" ]] || fail "Cannot find conda init script: ${conda_sh}"

  # shellcheck disable=SC1090
  source "${conda_sh}"

  local env_name
  env_name="$(resolve_conda_env_name)"
  conda env list | awk 'NR>2 {print $1}' | grep -Fxq "${env_name}" || fail "Conda env '${env_name}' not found. Run ./install.sh --conda${CONDA_ENV_NAME:+ --env-name ${CONDA_ENV_NAME}} first."

  conda activate "${env_name}"
}

run_target() {
  cd "${ROOT_DIR}"
  exec python -m ktxgo
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1 (use --help)"
      ;;
  esac
done

load_manager
case "${MANAGER}" in
  uv) activate_uv ;;
  conda) activate_conda ;;
  *) fail "Unsupported manager: ${MANAGER}" ;;
esac

run_target
