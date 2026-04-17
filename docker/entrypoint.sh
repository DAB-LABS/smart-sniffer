#!/usr/bin/env bash

envsubst < config.yaml.template > config.yaml
./BIN_NAME
