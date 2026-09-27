#!/bin/bash
set -eu
exec "$1" "$2" --root "$3" directories run
