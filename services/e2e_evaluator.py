"""
E2E Benchmark Evaluator — scores AI responses against ground truths.

Five scoring dimensions:
  1. Keyword matching (free) — required/forbidden keyword checks; supports list-of-alternatives per keyword
  2. Structural analysis (free) — format quality (code blocks, references, etc.)
  3. File/symbol mentions (free) — expected files and symbols referenced
  4. Factual accuracy (free) — verify specific claims against ground truth
  5. Completeness (free) — all required aspects addressed
  6. AI-as-judge (optional) — external AI rates the response
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

from services.e2e_tasks import GroundTruth


@dataclass
class EvalScore:
    """Detailed scoring breakdown for a single response."""
    keyword_score: float = 0.0
    structural_score: float = 0.0
    file_mention_score: float = 0.0
    factual_score: float = 0.0
    completeness_score: float = 0.0
    ai_judge_score: float | None = None
    combined_score: float = 0.0
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "keyword_score": round(self.keyword_score, 3),
            "structural_score": round(self.structural_score, 3),
            "file_mention_score": round(self.file_mention_score, 3),
            "factual_score": round(self.factual_score, 3),
            "completeness_score": round(self.completeness_score, 3),
            "ai_judge_score": round(self.ai_judge_score, 3) if self.ai_judge_score is not None else None,
            "combined_score": round(self.combined_score, 3),
            "details": self.details,
        }


class Evaluator:
    """Scores AI responses against ground truths using multiple dimensions."""

    def __init__(self, judge_cli: str | None = None, judge_model: str | None = None):
        self.judge_cli = judge_cli
        self.judge_model = judge_model

    def score(self, response_text: str, ground_truth: GroundTruth) -> EvalScore:
        """Score a response against ground truth using all available tiers."""
        result = EvalScore()
        response_lower = response_text.lower()

        # Tier 1: Keyword matching
        result.keyword_score, kw_details = self._keyword_score(response_lower, ground_truth)
        result.details["keywords"] = kw_details

        # Tier 2: Structural analysis
        result.structural_score, struct_details = self._structural_score(response_text)
        result.details["structural"] = struct_details

        # Tier 3: File/symbol mention scoring
        result.file_mention_score, file_details = self._file_mention_score(response_lower, ground_truth)
        result.details["file_mentions"] = file_details

        # Tier 4: Factual accuracy
        result.factual_score, fact_details = self._factual_score(response_lower, ground_truth)
        result.details["factual"] = fact_details

        # Tier 5: Completeness
        result.completeness_score, comp_details = self._completeness_score(response_lower, ground_truth)
        result.details["completeness"] = comp_details

        # Tier 6: AI-as-judge (optional)
        if self.judge_cli:
            result.ai_judge_score, judge_details = self._ai_judge_score(response_text, ground_truth)
            result.details["ai_judge"] = judge_details

        # Combine scores
        weights = ground_truth.scoring_weights
        if result.ai_judge_score is not None:
            # With judge: redistribute weights to include it
            total_w = sum(weights.values()) + 0.20
            result.combined_score = (
                weights.get("keyword", 0.15) / total_w * result.keyword_score +
                weights.get("structural", 0.10) / total_w * result.structural_score +
                weights.get("file_mention", 0.15) / total_w * result.file_mention_score +
                weights.get("factual", 0.35) / total_w * result.factual_score +
                weights.get("completeness", 0.25) / total_w * result.completeness_score +
                0.20 / total_w * result.ai_judge_score
            )
        else:
            total_w = sum(weights.values()) or 1.0
            result.combined_score = (
                weights.get("keyword", 0.15) / total_w * result.keyword_score +
                weights.get("structural", 0.10) / total_w * result.structural_score +
                weights.get("file_mention", 0.15) / total_w * result.file_mention_score +
                weights.get("factual", 0.35) / total_w * result.factual_score +
                weights.get("completeness", 0.25) / total_w * result.completeness_score
            )

        return result

    def _keyword_score(self, response_lower: str, truth: GroundTruth) -> tuple[float, dict]:
        """Check required keywords presence and forbidden keywords absence.

        Each element of required_keywords may be:
          - a str   — direct substring match
          - a list  — any alternative in the list matches (synonym group)
        """
        required = truth.required_keywords
        forbidden = truth.forbidden_keywords

        found_required = []
        missed_required = []
        for kw in required:
            if isinstance(kw, list):
                # Synonym group: at least one alternative must appear
                label = kw[0]
                if any(alt.lower() in response_lower for alt in kw):
                    found_required.append(label)
                else:
                    missed_required.append(label)
            else:
                if kw.lower() in response_lower:
                    found_required.append(kw)
                else:
                    missed_required.append(kw)

        found_forbidden = [kw for kw in forbidden if kw.lower() in response_lower]

        if not required and not forbidden:
            score = 0.5
        else:
            req_score = len(found_required) / len(required) if required else 1.0
            forbid_penalty = len(found_forbidden) / len(forbidden) * 0.5 if forbidden else 0.0
            score = max(0.0, req_score - forbid_penalty)

        return score, {
            "found_required": found_required,
            "missed_required": missed_required,
            "found_forbidden": found_forbidden,
        }

    def _structural_score(self, response: str) -> tuple[float, dict]:
        """Evaluate response structure quality."""
        score = 0.0
        details = {}

        code_blocks = re.findall(r"```[\s\S]*?```", response)
        details["code_blocks"] = len(code_blocks)
        if code_blocks:
            score += 0.2

        file_refs = re.findall(r"[a-zA-Z_][\w/\\]*\.\w{1,4}", response)
        details["file_references"] = len(file_refs)
        if file_refs:
            score += 0.2

        line_refs = re.findall(r"(?:line|L|:)\s*\d+", response, re.IGNORECASE)
        details["line_references"] = len(line_refs)
        if line_refs:
            score += 0.15

        backtick_refs = re.findall(r"`[a-zA-Z_]\w*`", response)
        details["symbol_references"] = len(backtick_refs)
        if backtick_refs:
            score += 0.15

        word_count = len(response.split())
        details["word_count"] = word_count
        if 50 <= word_count <= 600:
            score += 0.15
        elif 20 <= word_count < 50 or 600 < word_count <= 1000:
            score += 0.08
        elif word_count > 1000:
            score += 0.04  # Minimal credit for very long responses (comprehensive but verbose)

        has_structure = bool(re.search(r"^[\s]*[-*#\d]+[.)]?\s", response, re.MULTILINE))
        details["has_structure"] = has_structure
        if has_structure:
            score += 0.15

        return min(1.0, score), details

    def _file_mention_score(self, response_lower: str, truth: GroundTruth) -> tuple[float, dict]:
        """Check if expected files and symbols are mentioned."""
        expected_files = truth.expected_files
        expected_symbols = truth.expected_symbols

        found_files = []
        for f in expected_files:
            fname = f.replace("\\", "/").split("/")[-1].lower()
            if fname in response_lower or f.lower().replace("\\", "/") in response_lower.replace("\\", "/"):
                found_files.append(f)

        found_symbols = [s for s in expected_symbols if s.lower() in response_lower]

        total_expected = len(expected_files) + len(expected_symbols)
        total_found = len(found_files) + len(found_symbols)

        if total_expected == 0:
            score = 0.5
        else:
            score = total_found / total_expected

        return score, {
            "expected_files": expected_files,
            "found_files": found_files,
            "expected_symbols": expected_symbols,
            "found_symbols": found_symbols,
        }

    def _factual_score(self, response_lower: str, truth: GroundTruth) -> tuple[float, dict]:
        """Verify specific claims against ground truth. Higher = more accurate."""
        claims = truth.verifiable_claims
        if not claims:
            return 0.5, {"skipped": True, "reason": "no verifiable claims defined"}

        verified = []
        failed = []
        for claim_text, expected_true in claims:
            # Check if the claim's key elements appear in the response
            claim_lower = claim_text.lower()
            # Extract key terms from the claim (words > 3 chars)
            claim_terms = [w for w in re.findall(r"[a-z_]\w{3,}", claim_lower)
                           if w not in ("this", "that", "from", "with", "have", "does", "file",
                                        "uses", "calls", "also", "true", "false", "code", "class",
                                        "function", "value", "list", "test", "type", "name",
                                        "method", "return", "param", "object", "string", "line",
                                        "which", "when", "each", "been", "more", "into", "some")]
            if not claim_terms:
                continue

            # A claim is "verified" if most of its key terms appear in the response
            found_terms = sum(1 for t in claim_terms if t in response_lower)
            match_ratio = found_terms / len(claim_terms)

            if expected_true:
                # True claim: good if mentioned
                if match_ratio >= 0.5:
                    verified.append(claim_text)
                else:
                    failed.append(claim_text)
            else:
                # False claim: good if NOT mentioned
                if match_ratio < 0.5:
                    verified.append(claim_text)
                else:
                    failed.append(claim_text)

        total = len(verified) + len(failed)
        score = len(verified) / total if total else 0.5

        return score, {
            "verified_claims": verified,
            "failed_claims": failed,
            "total_claims": len(claims),
        }

    def _completeness_score(self, response_lower: str, truth: GroundTruth) -> tuple[float, dict]:
        """Check if all required aspects of the question were addressed."""
        aspects = truth.required_aspects
        if not aspects:
            return 0.5, {"skipped": True, "reason": "no required aspects defined"}

        # Map aspect names to detection patterns
        aspect_patterns = {
            "purpose": [r"purpose", r"responsible for", r"handles", r"used for", r"designed to"],
            "methods": [r"method", r"def\s", r"function"],
            "usage": [r"used by", r"called from", r"import", r"usage"],
            "imports": [r"import", r"from\s+\w+", r"depend"],
            "parameters": [r"param", r"argument", r"takes\s", r"accepts"],
            "location": [r"line\s*\d", r"at\s+line", r"found at", r"located"],
            "directories": [r"\bdirector(?:y|ies)\b", r"\bfolder\b", r"\bmodule\b", r"\bpackage\b", r"(?:services|cli|core|tests)/"],
            "responsibilities": [r"responsible", r"handles", r"manages", r"provides"],
            "relationships": [r"depends on", r"imports", r"calls", r"uses", r"relates"],
            "files": [r"\.py", r"\.js", r"\.ts", r"file"],
            "call_sites": [r"called from", r"referenced in", r"used in", r"calls\s"],
            "reasons": [r"because", r"in order to", r"for\s", r"to\s"],
            "import_chain": [r"import", r"from\s+", r"chain"],
            "data_flow": [r"passes", r"returns", r"flow", r"transforms", r"converts"],
            "transformations": [r"transform", r"convert", r"process", r"modify", r"format"],
            "error_handling": [r"error", r"exception", r"try", r"except", r"catch", r"raise"],
            "organization": [r"organiz", r"structure", r"split", r"module", r"separate"],
            "file_length": [r"\d+\s*lines", r"long\s+file", r"large"],
            "bare_except": [r"bare\s+except", r"except\s*:", r"catching\s+all"],
            "long_functions": [r"long\s+function", r"too\s+long", r"refactor", r"split"],
            "issues_found": [r"issue", r"problem", r"bug", r"concern", r"warning", r"anti.?pattern"],
            "locations": [
                r"line\s*\d", r"at\s+line", r"in\s+function", r"in\s+`\w",
                r"\bL\d{2,}", r"L\d+[–\-]\d+", r"#\s*\d{2,}", r":\s*\d{2,}",
                r"\(\s*L?\d{2,}\)", r"→\s*L?\d",
            ],
            "suggestions": [r"suggest", r"recommend", r"consider", r"should", r"could", r"fix\b", r"replace"],
            "duplication_patterns": [r"duplicat", r"repeat", r"similar", r"common\s+pattern"],
            "refactoring_approach": [r"refactor", r"extract", r"abstract", r"consolidat"],
            "shared_abstractions": [r"base\s+class", r"mixin", r"shared", r"common", r"abstract"],
        }

        addressed = []
        missed = []
        for aspect in aspects:
            patterns = aspect_patterns.get(aspect, [aspect])
            found = any(re.search(p, response_lower) for p in patterns)
            if found:
                addressed.append(aspect)
            else:
                missed.append(aspect)

        score = len(addressed) / len(aspects) if aspects else 0.5

        return score, {
            "addressed_aspects": addressed,
            "missed_aspects": missed,
            "total_aspects": len(aspects),
        }

    def _ai_judge_score(self, response: str, truth: GroundTruth) -> tuple[float, dict]:
        """Use an AI CLI as a judge to score the response quality."""
        judge_prompt = (
            "You are an expert code reviewer judging an AI's response about a codebase.\n\n"
            f"EXPECTED ANSWER SUMMARY: {truth.expected_answer_summary}\n\n"
            f"EXPECTED FILES: {', '.join(truth.expected_files)}\n\n"
            f"EXPECTED SYMBOLS: {', '.join(truth.expected_symbols)}\n\n"
            f"AI RESPONSE TO JUDGE:\n{response[:3000]}\n\n"
            "Rate the response on three dimensions (1-5 each):\n"
            "1. ACCURACY: Is the information correct and complete?\n"
            "2. RELEVANCE: Does it address the question with specific details?\n"
            "3. QUALITY: Is it well-organized and actionable?\n\n"
            "Reply with ONLY a JSON object: {\"accuracy\": N, \"relevance\": N, \"quality\": N}\n"
            "No other text."
        )

        # Clean env for nested CLI calls
        env = os.environ.copy()
        for block_var in ("CLAUDECODE", "CLAUDE_CODE", "GEMINI_CLI", "CODEX_CLI"):
            env.pop(block_var, None)

        try:
            cmd = self._build_judge_command(judge_prompt)
            t0 = time.perf_counter()
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=90, env=env,
                creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
            )
            latency = (time.perf_counter() - t0) * 1000

            output = result.stdout.strip()
            json_match = re.search(r"\{[^}]*\"accuracy\"[^}]*\}", output, re.DOTALL)
            if json_match:
                scores = json.loads(json_match.group())
                accuracy = float(scores.get("accuracy", 3))
                relevance = float(scores.get("relevance", 3))
                quality = float(scores.get("quality", 3))
                avg = (accuracy + relevance + quality) / 3.0
                normalized = (avg - 1) / 4.0
                return normalized, {
                    "accuracy": accuracy,
                    "relevance": relevance,
                    "quality": quality,
                    "latency_ms": round(latency, 1),
                    "judge_cli": self.judge_cli,
                }
        except Exception as e:
            return None, {"error": str(e)}

        return None, {"error": "Could not parse judge response"}

    def _build_judge_command(self, prompt: str) -> list[str]:
        """Build the CLI command for the AI judge."""
        if self.judge_cli == "claude":
            cmd = ["claude", "-p", prompt, "--output-format", "text"]
            if self.judge_model:
                cmd += ["--model", self.judge_model]
            return cmd
        elif self.judge_cli == "gemini":
            cmd = ["gemini", "-p", prompt, "--output-format", "text"]
            if self.judge_model:
                cmd += ["-m", self.judge_model]
            return cmd
        elif self.judge_cli == "codex":
            cmd = ["codex", "exec", prompt]
            if self.judge_model:
                cmd += ["--model", self.judge_model]
            return cmd
        else:
            raise ValueError(f"Unknown judge CLI: {self.judge_cli}")
