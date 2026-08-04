#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="${0:A:h}"
PROJECT_DIR="${SCRIPT_DIR:h}"
PLUGIN_UUID="com.yanndouchin.founderos-actions"
PLUGIN_NAME="${PLUGIN_UUID}.sdPlugin"
SOURCE_DIR="${PROJECT_DIR}/${PLUGIN_NAME}"
PLUGIN_PARENT="${HOME}/Library/Application Support/com.elgato.StreamDeck/Plugins"
DESTINATION="${PLUGIN_PARENT}/${PLUGIN_NAME}"
CLI="${PROJECT_DIR}/node_modules/.bin/streamdeck"

if [[ ! -d "${SOURCE_DIR}" || ! -f "${SOURCE_DIR}/manifest.json" ]]; then
  print -u2 "Plugin source was not found."
  exit 2
fi
if [[ ! -x "${CLI}" ]]; then
  print -u2 "Dependencies are missing. Run npm ci in ${PROJECT_DIR}."
  exit 2
fi
cd "${PROJECT_DIR}"
npm run check
npm test
node scripts/preflight-helper.mjs
npm run build
"${CLI}" validate --no-update-check "${SOURCE_DIR}"

/bin/mkdir -p "${PLUGIN_PARENT}"
STAGING_ROOT="$(/usr/bin/mktemp -d "${TMPDIR:-/tmp}/founderos-streamdeck-install.XXXXXX")"
STAGED_PLUGIN="${STAGING_ROOT}/${PLUGIN_NAME}"
BACKUP="${PLUGIN_PARENT}/.${PLUGIN_NAME}.backup.$$"

cleanup() {
  if [[ "${STAGING_ROOT}" == */founderos-streamdeck-install.* && -d "${STAGING_ROOT}" ]]; then
    /bin/rm -rf "${STAGING_ROOT}"
  fi
}
trap cleanup EXIT

/usr/bin/ditto "${SOURCE_DIR}" "${STAGED_PLUGIN}"
"${CLI}" validate --no-update-check "${STAGED_PLUGIN}"

HAD_PREVIOUS=0
if [[ -e "${DESTINATION}" || -L "${DESTINATION}" ]]; then
  /bin/mv "${DESTINATION}" "${BACKUP}"
  HAD_PREVIOUS=1
fi

if ! /bin/mv "${STAGED_PLUGIN}" "${DESTINATION}"; then
  if (( HAD_PREVIOUS )); then
    /bin/mv "${BACKUP}" "${DESTINATION}"
  fi
  print -u2 "The plugin could not be copied. The previous installation was restored."
  exit 3
fi

if ! "${CLI}" restart "${PLUGIN_UUID}"; then
  FAILED_PLUGIN="${STAGING_ROOT}/${PLUGIN_NAME}.failed"
  /bin/mv "${DESTINATION}" "${FAILED_PLUGIN}"
  if (( HAD_PREVIOUS )); then
    /bin/mv "${BACKUP}" "${DESTINATION}"
    "${CLI}" restart "${PLUGIN_UUID}" >/dev/null 2>&1 || true
  fi
  print -u2 "The plugin could not be restarted. The previous installation was restored."
  exit 4
fi

print "FounderOS Actions was installed and restarted successfully."
if (( HAD_PREVIOUS )); then
  print "The previous installation backup was retained at ${BACKUP}."
fi
