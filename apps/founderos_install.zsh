#!/bin/zsh
set -euo pipefail

umask 077

script_directory="${0:A:h}"
repository_root="${script_directory:h}"
runtime_state="${FOUNDEROS_STATE_DIR:-${HOME}/Library/Application Support/FounderOS}"
bootstrap_root="${runtime_state}/bootstrap"
config_source="founderos.local.json"
config_replaced=0
rewritten_arguments=()
argument_index=1

while (( argument_index <= $# )); do
  argument="${@[argument_index]}"
  if [[ "${argument}" == "--config" ]]; then
    next_index=$((argument_index + 1))
    if (( next_index > $# )); then
      print -u2 "error: --config requires a path"
      exit 2
    fi
    config_source="${@[next_index]}"
    rewritten_arguments+=("--config" "__FOUNDEROS_STAGED_CONFIG__")
    config_replaced=1
    argument_index=$((argument_index + 2))
    continue
  fi
  if [[ "${argument}" == --config=* ]]; then
    config_source="${argument#--config=}"
    rewritten_arguments+=("--config=__FOUNDEROS_STAGED_CONFIG__")
    config_replaced=1
    argument_index=$((argument_index + 1))
    continue
  fi
  rewritten_arguments+=("${argument}")
  argument_index=$((argument_index + 1))
done

if [[ "${config_source}" != /* ]]; then
  config_source="${PWD}/${config_source}"
fi
if [[ ! -f "${config_source}" ]]; then
  print -u2 "error: FounderOS configuration was not found"
  exit 2
fi
if [[ ! -f "${repository_root}/web/dist/index.html" ]]; then
  print -u2 "error: BUSY Bar emulator frontend is not built; run npm run build first"
  exit 2
fi

/bin/mkdir -p "${bootstrap_root}"
/bin/chmod 700 "${runtime_state}" "${bootstrap_root}"
temporary_directory="$(/usr/bin/mktemp -d "${bootstrap_root}/.install.XXXXXX")"

cleanup() {
  if [[ -n "${temporary_directory:-}" && "${temporary_directory}" == "${bootstrap_root}/.install."* ]]; then
    /bin/rm -rf -- "${temporary_directory}"
  fi
}
trap cleanup EXIT INT TERM

archive_path="${temporary_directory}/runtime.tar"
/usr/bin/git -C "${repository_root}" archive \
  --format=tar \
  "--output=${archive_path}" \
  HEAD \
  -- \
  founder_os \
  apps \
  public/brand \
  public/fonts \
  public/icons \
  public/sounds \
  public/icons.json \
  server.js
/usr/bin/tar -xf "${archive_path}" -C "${temporary_directory}"
/bin/rm "${archive_path}"

/bin/mkdir -p "${temporary_directory}/web"
/bin/cp -R "${repository_root}/web/dist" "${temporary_directory}/web/dist"
staged_config="${temporary_directory}/founderos.runtime.json"
/bin/cp "${config_source}" "${staged_config}"

/usr/bin/find "${temporary_directory}" -type d -exec /bin/chmod 700 {} +
/usr/bin/find "${temporary_directory}" -type f -exec /bin/chmod 600 {} +

if (( config_replaced == 0 )); then
  rewritten_arguments=("--config" "${staged_config}" "${rewritten_arguments[@]}")
else
  for (( rewrite_index = 1; rewrite_index <= ${#rewritten_arguments}; rewrite_index++ )); do
    if [[ "${rewritten_arguments[rewrite_index]}" == "__FOUNDEROS_STAGED_CONFIG__" ]]; then
      rewritten_arguments[rewrite_index]="${staged_config}"
    elif [[ "${rewritten_arguments[rewrite_index]}" == "--config=__FOUNDEROS_STAGED_CONFIG__" ]]; then
      rewritten_arguments[rewrite_index]="--config=${staged_config}"
    fi
  done
fi

python_executable="$(command -v python3)"
FOUNDEROS_BOOTSTRAPPED=1 "${python_executable}" \
  "${temporary_directory}/apps/founderosctl.py" \
  "${rewritten_arguments[@]}"
