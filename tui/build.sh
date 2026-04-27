#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
echo "Building C3 TUI..."
go mod tidy
go build -ldflags="-s -w" -o c3 .
echo "Build successful: tui/c3"
