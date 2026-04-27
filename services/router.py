"""Adaptive Multi-Model Router — Classifies queries and routes to appropriate local LLM.

Feature extraction + classification:
- log_summary    → gemma3n:latest (temp 0.1)   — large log/output summarization
- simple_qa      → deepseek-r1:1.5b (temp 0.4)  — short factual questions
- complex        → llama3.2:3b (temp 0.5) — multi-step reasoning
- passthrough    → Claude (no local routing)
"""
import re
import threading
import time

from core import count_tokens
from services.ollama_client import OllamaClient

# Route class definitions with model assignments
ROUTE_CLASSES = {
    "classification": {
        "default_model": "qwen2:0.5b",
        "temperature": 0.0,
        "max_tokens": 128,
        "num_ctx": 2048,
        "description": "Ultra-fast classification and feature extraction (Nano Tier)",
    },
    "log_summary": {
        "default_model": "gemma3n:latest",
        "temperature": 0.1,
        "max_tokens": 300,
        "num_ctx": 8192,
        "description": "Large log/output summarization",
    },
    "simple_qa": {
        "default_model": "deepseek-r1:1.5b",
        "temperature": 0.4,
        "max_tokens": 256,
        "num_ctx": 4096,
        "description": "Short factual questions",
    },
    "complex": {
        "default_model": "llama3.2:3b",
        "temperature": 0.5,
        "max_tokens": 512,
        "num_ctx": 8192,
        "description": "Multi-step reasoning",
    },
    "passthrough": {
        "default_model": None,
        "temperature": None,
        "max_tokens": None,
        "description": "Route to Claude (no local model)",
    },
}

# ── Feature extraction patterns ──────────────────────────
_STACKTRACE_RE = re.compile(
    r'Traceback|at\s+\w+\.\w+\(|File\s+".*",\s+line\s+\d+'
    r'|Exception|Error:|panic:|FAIL',
    re.IGNORECASE,
)
_CODE_RE = re.compile(r'[{}\[\]();=<>]|def\s|class\s|function\s|import\s|const\s|let\s|var\s')
_FILE_REF_RE = re.compile(r'[\w/\\]+\.\w{1,5}(?::\d+)?')
_QUESTION_RE = re.compile(r'\?\s*$|^(what|how|why|when|where|which|can|does|is|are)\s', re.IGNORECASE)


def _resolve_model_name(candidate: str, available: list[str]) -> str:
    """Resolve configured model alias to an installed Ollama model name."""
    if not candidate:
        return ""
    normalized = candidate.strip().lower()
    if not normalized:
        return ""

    for model in available:
        if model.lower() == normalized:
            return model

    base = normalized.split(":", 1)[0]
    for model in available:
        lower = model.lower()
        if lower == base or lower.startswith(base + ":"):
            return model

    for model in available:
        if base in model.lower():
            return model

    return ""


def _route_fallback_order(route_class: str) -> list[str]:
    """Conservative fallback model order per route class."""
    if route_class == "simple_qa":
        return ["llama3.2:latest", "llama3.2:3b", "qwen3-coder-next:latest", "gemma3n:latest"]
    if route_class == "complex":
        return ["llama3.2:latest", "qwen3-coder-next:latest", "gemma3n:latest"]
    if route_class == "log_summary":
        return ["gemma3n:latest", "llama3.2:latest", "llama3.2:3b"]
    return ["llama3.2:latest", "gemma3n:latest"]


class ModelRouter:
    """Classifies input and routes to appropriate Ollama model."""

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        base_url = self.config.get("ollama_base_url", "http://localhost:11434")
        self.ollama = OllamaClient(base_url)
        self.log_threshold = self.config.get("router_log_threshold", 500)
        self.simple_threshold = self.config.get("router_simple_threshold", 100)
        self.allow_model_fallback = self.config.get("router_allow_model_fallback", True)
        fb = self.config.get("router_fallback_models", [])
        self.router_fallback_models = fb if isinstance(fb, list) else ([fb] if fb else [])
        self.retry_on_empty = self.config.get("router_retry_on_empty", True)
        self._lock = threading.Lock()

        # Model overrides from config
        self._model_overrides = {}
        for cls_name in ROUTE_CLASSES:
            config_key = f"{cls_name}_model"
            if config_key in self.config:
                self._model_overrides[cls_name] = self.config[config_key]

        # Metrics
        self.metrics = {
            "total_routes": 0,
            "by_class": {cls: 0 for cls in ROUTE_CLASSES},
            "failures": 0,
            "total_latency_ms": 0,
            "avg_latency_ms": 0,
        }

    def classify(self, query: str, context: str = "") -> dict:
        """Classify the query into a route class using local AI (Nano Tier) or heuristics."""
        full_text = query + "\n" + context if context else query
        features = self._extract_features(full_text)

        # 1. Try AI-powered classification (Nano Tier)
        ai_class = None
        nano_config = ROUTE_CLASSES["classification"]
        if self.ollama.is_available() and self.ollama.has_model(nano_config["default_model"]):
            ai_class = self._ai_classify(query, context)

        # 2. Fallback to heuristic classification
        route_class = ai_class or self._classify_features(features)

        route_info = ROUTE_CLASSES[route_class]
        model = self._model_overrides.get(route_class, route_info["default_model"])

        return {
            "route_class": route_class,
            "features": features,
            "model": model,
            "temperature": route_info["temperature"],
            "max_tokens": route_info["max_tokens"],
            "num_ctx": route_info.get("num_ctx", 4096),
            "description": route_info["description"],
            "classification_source": "ai" if ai_class else "heuristic"
        }

    def _ai_classify(self, query: str, context: str = "") -> str | None:
        """Use Nano model to classify the query."""
        nano_model = ROUTE_CLASSES["classification"]["default_model"]

        # Build classification prompt
        class_desc = "\n".join([f"- {k}: {v['description']}" for k, v in ROUTE_CLASSES.items() if k != "classification"])
        system = (
            "You are a routing classifier for a coding assistant. "
            "Output ONLY the category name from this list:\n"
            f"{class_desc}\n\n"
            "Rules:\n"
            "1. If it's a short question, use 'simple_qa'.\n"
            "2. If it involves complex reasoning or bug analysis, use 'complex'.\n"
            "3. If it's a large log or terminal output to summarize, use 'log_summary'.\n"
            "4. If it's a direct code instruction better handled by the primary model, use 'passthrough'.\n"
            "Output EXACTLY one word."
        )

        try:
            # Use ultra-low max_tokens and num_ctx for speed
            response = self.ollama.generate(
                prompt=f"Input: {query[:500]}",
                model=nano_model,
                system=system,
                temperature=0.0,
                max_tokens=10,
                num_ctx=1024
            )
            if not response:
                return None

            # Sanitize response
            found = response.strip().lower()
            for cls_name in ROUTE_CLASSES:
                if cls_name in found:
                    return cls_name
            return None
        except Exception:
            return None

    def route(self, query: str, context: str = "",
              force_class: str = "", stream: bool = False) -> dict:
        """Classify and execute routing to the appropriate model.

        If force_class is set, skip classification and use that class.
        Returns dict with: route_class, model, response, latency_ms, features
        """
        if self.config.get("HYBRID_DISABLE_TIER2"):
            return {
                "route_class": "passthrough",
                "model": None,
                "response": None,
                "latency_ms": 0,
                "reason": "Tier 2 disabled",
            }

        # Classify
        if force_class and force_class in ROUTE_CLASSES:
            classification = {
                "route_class": force_class,
                "features": self._extract_features(query),
                **ROUTE_CLASSES[force_class],
            }
            model = self._model_overrides.get(force_class, ROUTE_CLASSES[force_class]["default_model"])
            classification["model"] = model
        else:
            classification = self.classify(query, context)

        route_class = classification["route_class"]
        model = classification.get("model")

        # Passthrough — don't call any local model
        if route_class == "passthrough" or model is None:
            with self._lock:
                self.metrics["total_routes"] += 1
                self.metrics["by_class"]["passthrough"] += 1
            return {
                "route_class": "passthrough",
                "model": None,
                "response": None,
                "latency_ms": 0,
                "features": classification.get("features", {}),
            }

        available = self.ollama.list_models() or []
        candidates = []

        resolved_primary = _resolve_model_name(model, available)
        if resolved_primary:
            candidates.append(resolved_primary)
        elif model and not available:
            # If inventory lookup fails, still attempt requested model.
            candidates.append(model)

        if self.allow_model_fallback and available:
            for cand in _route_fallback_order(route_class) + self.router_fallback_models + available:
                resolved = _resolve_model_name(cand, available)
                if resolved and resolved not in candidates:
                    candidates.append(resolved)

        if model and model not in candidates:
            candidates.append(model)

        # Route to local model (with fallback attempts)
        start = time.monotonic()
        system = self._get_system_prompt(route_class)
        response = None
        used_model = model
        for candidate in candidates:
            used_model = candidate
            response = self.ollama.generate(
                prompt=query if not context else f"{query}\n\nContext:\n{context}",
                model=candidate,
                system=system,
                temperature=classification.get("temperature", 0.3),
                max_tokens=classification.get("max_tokens", 512),
                num_ctx=classification.get("num_ctx", 4096),
                stream=stream,
            )
            if response is not None:
                break
            if not self.retry_on_empty:
                break

        latency_ms = int((time.monotonic() - start) * 1000)

        with self._lock:
            self.metrics["total_routes"] += 1
            self.metrics["by_class"][route_class] += 1
            if response is None:
                self.metrics["failures"] += 1
            self.metrics["total_latency_ms"] += latency_ms
            total = self.metrics["total_routes"]
            self.metrics["avg_latency_ms"] = self.metrics["total_latency_ms"] // max(total, 1)

        return {
            "route_class": route_class,
            "model": used_model,
            "response": response,
            "latency_ms": latency_ms,
            "features": classification.get("features", {}),
        }

    def summarize(self, text: str, style: str = "concise", stream: bool = False) -> dict:
        """Summarize text using the appropriate model based on length.

        style: 'concise' (1-3 lines), 'detailed' (5-10 lines), 'bullet' (bullet points)
        """
        tokens = count_tokens(text)

        # Pick model based on text size
        if tokens > self.log_threshold:
            model = self._model_overrides.get("log_summary", "gemma3n:latest")
            temp = 0.1
        else:
            model = self._model_overrides.get("simple_qa", "deepseek-r1:1.5b")
            temp = 0.3

        style_prompts = {
            "concise": "Summarize in 1-3 lines. Be extremely terse.",
            "detailed": "Summarize in 5-10 lines. Cover key points.",
            "bullet": "Summarize as 3-7 bullet points.",
        }
        system = f"You are a summarizer. {style_prompts.get(style, style_prompts['concise'])}"

        start = time.monotonic()
        response = self.ollama.generate(
            prompt=f"Summarize:\n\n{text[:4000]}",
            model=model,
            system=system,
            temperature=temp,
            max_tokens=300,
            stream=stream,
        )
        latency_ms = int((time.monotonic() - start) * 1000)

        return {
            "summary": response,
            "model": model,
            "style": style,
            "input_tokens": tokens,
            "latency_ms": latency_ms,
        }

    def get_metrics(self) -> dict:
        """Return routing metrics."""
        with self._lock:
            return dict(self.metrics)

    # ── Feature extraction ───────────────────────────────

    def _extract_features(self, text: str) -> dict:
        """Extract classification features from input text."""
        tokens = count_tokens(text)
        lines = text.splitlines()
        code_lines = sum(1 for line in lines if _CODE_RE.search(line))
        total_lines = max(len(lines), 1)

        return {
            "input_tokens": tokens,
            "code_ratio": round(code_lines / total_lines, 2),
            "has_stacktrace": bool(_STACKTRACE_RE.search(text)),
            "file_count": len(set(_FILE_REF_RE.findall(text))),
            "is_question": bool(_QUESTION_RE.search(text.strip()[:200])),
            "line_count": total_lines,
        }

    def _classify_features(self, features: dict) -> str:
        """Classify based on extracted features."""
        tokens = features["input_tokens"]
        code_ratio = features["code_ratio"]
        has_stacktrace = features["has_stacktrace"]
        is_question = features["is_question"]

        # Large output with low code ratio → log summary
        if tokens > self.log_threshold and code_ratio < 0.3:
            return "log_summary"

        # Has stacktrace → likely needs detailed analysis
        if has_stacktrace and tokens > 200:
            return "complex"

        # Short question → simple QA
        if is_question and tokens < self.simple_threshold:
            return "simple_qa"

        # Short, code-heavy → passthrough to Claude
        if code_ratio > 0.5:
            return "passthrough"

        # Medium complexity
        if tokens > self.simple_threshold:
            return "complex"

        # Default: let Claude handle it
        return "passthrough"

    def _get_system_prompt(self, route_class: str) -> str:
        """Get the system prompt for a route class."""
        prompts = {
            "log_summary": (
                "You summarize logs and terminal output. Be concise. "
                "Highlight errors, warnings, and key results. "
                "Preserve file paths and line numbers from errors."
            ),
            "simple_qa": (
                "You answer short factual questions concisely. "
                "Give direct answers without preamble."
            ),
            "complex": (
                "You analyze code and technical problems. "
                "Think step by step. Be thorough but concise."
            ),
        }
        return prompts.get(route_class, "")
