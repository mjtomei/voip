#!/bin/bash
set -eu
exec "$1" "$2" --root "$3" --session "${4:-default}" request run
