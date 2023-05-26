#!/bin/bash
set -o errexit   # abort on nonzero exitstatus
set -o nounset   # abort on unbound variable
set -o pipefail  # don't hide errors within pipes

cd "$(realpath "$(dirname "${BASH_SOURCE[0]}")")"

docker \
	build \
	-t moderately-helpful-bot:local \
	.
