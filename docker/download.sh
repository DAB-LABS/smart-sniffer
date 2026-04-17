#!/usr/bin/env bash

BIN_NAME="${1:?Error: bin_name is required}"
PLATFORM="${2:?Error: platform is required}"
VERSION="${3:?Error: release version is required}"

echo ${BIN_NAME}
echo ${PLATFORM}
echo ${VERSION}

if [ "x$VERSION" = "xlatest" ]; then
  VERSION=$(curl -sSf "https://api.github.com/repos/DAB-LABS/smart-sniffer/releases/latest" \
    | grep '"tag_name"' | head -1 | sed 's/.*"v\([^"]*\)".*/\1/')
  if [ -z "$VERSION" ]; then
    fail "Could not determine latest version. Set VERSION=x.y.z manually."
  fi
fi
echo ${VERSION}


curl -sSfL -o ${BIN_NAME} https://github.com/DAB-LABS/smart-sniffer/releases/download/v${VERSION}/smartha-agent-linux-${PLATFORM}
chmod +x ${BIN_NAME}
sed -i "s/BIN_NAME/${BIN_NAME}/g" entrypoint.sh
