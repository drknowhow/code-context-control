#!/bin/bash
# C3 — Claude Code Companion Installer
# Installs c3 as a globally available command

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Get version from cli/c3.py
C3_VER=$(grep "__version__ =" "$SCRIPT_DIR/cli/c3.py" | cut -d'"' -f2)

echo "╔══════════════════════════════════════════════╗"
echo "║   C3 — Claude Code Companion Installer       ║"
echo "║   Version: v$C3_VER                              ║"
echo "╚══════════════════════════════════════════════╝"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 is required. Please install it first."
    exit 1
fi

echo "📦 Installing C3 (this may take a minute)..."
# pip install creates the `c3`, `c3-mcp`, and `c3-hub` entry-point scripts in
# the active Python's bin dir (or ~/.local/bin with --user). Includes the
# optional [tui] extra so `c3` with no args launches the Textual UI.
pip3 install "$SCRIPT_DIR[tui]" -q 2>/dev/null \
  || pip3 install --user "$SCRIPT_DIR[tui]" -q 2>/dev/null \
  || pip3 install --break-system-packages "$SCRIPT_DIR[tui]" -q

# Sanity-check that `c3` is discoverable on PATH
if ! command -v c3 &> /dev/null; then
    echo "⚠️  'c3' was installed but is not on your PATH."
    echo "    Add your Python user-bin to PATH, e.g.:"
    echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

echo ""
echo "✅ C3 installed successfully!"
echo ""
echo "Quick start:"
echo "  cd /your/project"
echo "  c3 init ."
echo "  c3 init . --force                  # Existing C3 project: apply latest migrations"
echo "  c3 install-mcp .                   # Register MCP tools for your IDE (auto-detect)"
echo "  c3 ui                              # Launch per-project web dashboard"
echo "  c3-hub                             # Launch global Project Hub (port 3330)"
echo "  c3 stats                           # CLI stats"
echo "  c3 context 'fix the auth bug'      # Get context"
echo "  c3 pipe 'fix the auth bug'         # All-in-one context pipeline"
echo ""
echo "Bitbucket Data Center / Server (v2.30.0+, optional):"
echo "  c3 bitbucket login --url https://bitbucket.example.com   # Stores PAT in OS keyring"
echo "  c3 bitbucket set-default --project PROJ --repo my-repo   # Pin default workspace"
echo "  c3 bitbucket status                                       # Show accounts + connectivity"
echo "  See guide/bitbucket.html for the full action reference."
echo ""
echo "Run 'c3 --help' for all commands."

