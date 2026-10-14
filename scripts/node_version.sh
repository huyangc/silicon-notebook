# Sourced (not executed) by the scripts that run or bundle Node.js.
#
# The repository supports exactly one Node.js major: the one in the repository
# root's .nvmrc. CI's setup-node reads that same file, frontend/package.json's
# "engines" plus frontend/.npmrc's engine-strict make npm ci/install refuse any
# other major, and these scripts refuse it before doing any work, so a wrong
# version fails loudly instead of producing a build or a test run nobody
# verified.
#
#   require_node_major <version> <what needs it>
#
# <version> is `node -v` output or a NODE_VERSION value (with or without the
# leading "v"). Prints one line to stderr and returns 1 on a mismatch.
require_node_major() {
  local root required version
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  required="$(tr -d '[:space:]v' < "$root/.nvmrc")"
  version="${1#v}"
  if [[ -z "$version" || ( "$version" != "$required" && "$version" != "$required".* ) ]]; then
    printf '%s needs Node.js %s.x (pinned in .nvmrc); found %s\n' \
      "$2" "$required" "${1:-no node}" >&2
    return 1
  fi
}
