#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
integration_root="${repo_root}/integrations/deepseek-harness"
profile_name="${1:-web}"
dsh_home_root="${DSH_HOME:-${HOME}/.dsh}"
preset_root="${dsh_home_root}/.agent-presets"
workspace_root="${RESEARCH_HARNESS_WORKSPACE:-${HOME}/research-workspaces/default}"

command -v dsh >/dev/null 2>&1 || {
  echo "dsh is not installed; install @deepseek-ai/dsh first" >&2
  exit 2
}

python_executable="${RESEARCH_HARNESS_PYTHON:-$(command -v python3 || command -v python)}"
"${python_executable}" -c "import research_artifacts" >/dev/null

npm --prefix "${integration_root}" run build
dsh plugin --profile "${profile_name}" add "${integration_root}"

mkdir -p "${preset_root}"
for source in "${integration_root}"/presets/research-*; do
  name="$(basename "${source}")"
  target="${preset_root}/${name}"
  if [[ -e "${target}" ]]; then
    echo "preserving existing preset: ${target}" >&2
    continue
  fi
  cp -R "${source}" "${target}"
done

cat <<EOF
Research Harness plugin installed in dsh profile '${profile_name}'.

Initialize the data workspace (new or empty directory):
  research --workspace '${workspace_root}' workspace-init

Start the host with:
  export RESEARCH_HARNESS_WORKSPACE='${workspace_root}'
  export RESEARCH_HARNESS_PYTHON='${python_executable}'
  export RESEARCH_HARNESS_SOCKET='${workspace_root}/.harness/research/cordis.sock'
  dsh --profile '${profile_name}'

Then call the host-side operator with:
  '${repo_root}/scripts/research-cordis' --socket '${workspace_root}/.harness/research/cordis.sock' status PROJECT
EOF
