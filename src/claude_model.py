"""Claude stand-in for google.generativeai.GenerativeModel.

The pipeline calls ``model.generate_content(prompt, generation_config=...)`` and
reads ``.text``. This class keeps that shape so the rest of the code is unchanged.
Key: credentials/anthropic.key (one line). Model/effort: config.json "claude".
"""
import json
import logging
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_KEY_FILE = _ROOT / "credentials" / "anthropic.key"
_USAGE_LOG = _ROOT / "logs" / "claude_usage.log"
# Set once the workspace spend cap is hit; the caller's key rotation then
# rebuilds the model and gets Gemini for the rest of this process.
_capped = False


def _settings() -> dict:
    try:
        return json.load(open(_ROOT / "config.json", encoding="utf-8")).get("claude", {})
    except Exception:
        return {}


def claude_enabled() -> bool:
    return not _capped and bool(_settings().get("enabled")) and _KEY_FILE.exists()


class _Response:
    def __init__(self, text: str):
        self.text = text


class ClaudeModel:
    def __init__(self):
        cfg = _settings()
        self.model_name = cfg.get("model", "claude-opus-5-5")
        self.effort = cfg.get("effort", "low")
        self.client = anthropic.Anthropic(api_key=_KEY_FILE.read_text().strip(), timeout=600)

    def generate_content(self, contents, generation_config=None, **_ignored):
        prompt = contents if isinstance(contents, str) else "\n".join(str(c) for c in contents)
        max_out = getattr(generation_config, "max_output_tokens", None) or 8192
        try:
            response = self.client.messages.create(
                model=self.model_name,
                # thinking is always on and counts toward this cap
                max_tokens=max(int(max_out), 16000),
                output_config={"effort": self.effort},
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.BadRequestError as e:
            if "usage limits" not in str(e):
                raise
            global _capped
            _capped = True
            logger.warning("Claude spend cap reached, switching to Gemini")
            # "quota" makes the caller rotate keys, which lands on Gemini
            raise RuntimeError(f"claude quota: {e}") from e
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        text = "".join(b.text for b in response.content if b.type == "text")
        u = response.usage
        try:
            with open(_USAGE_LOG, "a", encoding="utf-8") as f:
                f.write(f"{self.model_name} in={u.input_tokens} out={u.output_tokens} stop={response.stop_reason}\n")
        except Exception:
            pass
        if not text:
            raise RuntimeError(f"Claude returned no text (stop_reason={response.stop_reason})")
        return _Response(text)
