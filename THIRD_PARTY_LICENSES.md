# Third-Party Licenses

Code Context Control (C3) bundles or depends on the following third-party
components. Each is distributed under the terms of its own license. Full
license texts are available in the linked upstream projects.

| Package | License | Upstream |
|---|---|---|
| tiktoken | MIT | https://github.com/openai/tiktoken |
| fastmcp | Apache-2.0 | https://github.com/jlowin/fastmcp |
| watchdog | Apache-2.0 | https://github.com/gorakhargosh/watchdog |
| tree-sitter | MIT | https://github.com/tree-sitter/py-tree-sitter |
| tree-sitter-python | MIT | https://github.com/tree-sitter/tree-sitter-python |
| tree-sitter-javascript | MIT | https://github.com/tree-sitter/tree-sitter-javascript |
| tree-sitter-typescript | MIT | https://github.com/tree-sitter/tree-sitter-typescript |
| tree-sitter-html | MIT | https://github.com/tree-sitter/tree-sitter-html |
| tree-sitter-markdown | MIT | https://github.com/tree-sitter-grammars/tree-sitter-markdown |
| tree-sitter-css | MIT | https://github.com/tree-sitter/tree-sitter-css |
| tree-sitter-go | MIT | https://github.com/tree-sitter/tree-sitter-go |
| tree-sitter-rust | MIT | https://github.com/tree-sitter/tree-sitter-rust |
| tree-sitter-json | MIT | https://github.com/tree-sitter/tree-sitter-json |
| tree-sitter-yaml | MIT | https://github.com/tree-sitter-grammars/tree-sitter-yaml |
| Flask | BSD-3-Clause | https://github.com/pallets/flask |
| scikit-learn | BSD-3-Clause | https://github.com/scikit-learn/scikit-learn |
| NumPy | BSD-3-Clause | https://github.com/numpy/numpy |
| Rich | MIT | https://github.com/Textualize/rich |
| Click | BSD-3-Clause | https://github.com/pallets/click |
| PyYAML | MIT | https://github.com/yaml/pyyaml |
| ChromaDB | Apache-2.0 | https://github.com/chroma-core/chroma |
| Textual (optional) | MIT | https://github.com/Textualize/textual |

## Optional / runtime-loaded components

- **Ollama** (Apache-2.0) — used as an optional local-inference backend.
  Models loaded via Ollama are governed by their own respective licenses
  (e.g. Llama Community License, Apache-2.0, MIT). Users are responsible for
  complying with the license terms of any model they download.
- **Anthropic Claude API** — accessed only when explicitly configured by the
  end user; subject to Anthropic's terms of service.
- **OpenAI Codex CLI**, **Google Gemini CLI** — optional integrations
  invoked only when the user opts in.

If you believe a dependency is missing or incorrectly attributed, please
open an issue or email `dtselenc@gmail.com`.
