"""Claude stand-in for google.generativeai.GenerativeModel.

The pipeline calls ``model.generate_content(prompt, generation_config=...)`` and
reads ``.text``. This class keeps that shape so the rest of the code is unchanged.
Key: credentials/anthropic.key (one line). Model/effort: config.json "claude".
With "driver": "cli" it runs the Claude Code CLI on the subscription instead of the
API key: token from `claude setup-token` in credentials/claude_oauth.token.
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

import anthropic

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_KEY_FILE = _ROOT / "credentials" / "anthropic.key"
_TOKEN_FILE = _ROOT / "credentials" / "claude_oauth.token"
_USAGE_LOG = _ROOT / "logs" / "claude_usage.log"
# Clean HOME for the CLI so no CLAUDE.md, hooks or MCP servers reach the prompt.
_CLI_HOME = Path(os.environ.get("CLAUDE_CLI_HOME", _ROOT / ".claude-cli-home"))
# Set once the workspace spend cap is hit; the caller's key rotation then
# rebuilds the model and gets Gemini for the rest of this process.
_capped = False


def _settings() -> dict:
    try:
        return json.load(open(_ROOT / "config.json", encoding="utf-8")).get("claude", {})
    except Exception:
        return {}


def _use_cli() -> bool:
    return _settings().get("driver") == "cli"


def claude_enabled() -> bool:
    if _capped or not _settings().get("enabled"):
        return False
    if _use_cli():
        return _TOKEN_FILE.exists() or (_CLI_HOME / ".claude" / ".credentials.json").exists()
    return _KEY_FILE.exists()


def _log_usage(line: str) -> None:
    try:
        with open(_USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _quota(msg: str):
    global _capped
    _capped = True
    logger.warning("Claude limit reached, switching to Gemini")
    # "quota" makes the caller rotate keys, which lands on Gemini
    return RuntimeError(f"claude quota: {msg}")


class _Response:
    def __init__(self, text: str):
        self.text = text


class ClaudeModel:
    def __init__(self):
        cfg = _settings()
        self.model_name = cfg.get("model", "claude-opus-5-5")
        self.effort = cfg.get("effort", "low")
        self.cli = _use_cli()
        if not self.cli:
            self.client = anthropic.Anthropic(api_key=_KEY_FILE.read_text().strip(), timeout=600)

    def _generate_cli(self, prompt: str) -> "_Response":
        _CLI_HOME.mkdir(mode=0o700, exist_ok=True)
        env = {**os.environ, "HOME": str(_CLI_HOME)}
        env.pop("ANTHROPIC_API_KEY", None)  # an API key would win over the subscription
        if _TOKEN_FILE.exists():
            env["CLAUDE_CODE_OAUTH_TOKEN"] = _TOKEN_FILE.read_text().strip()
        cmd = ["claude", "-p", "--model", self.model_name, "--effort", self.effort,
               "--tools", "", "--no-session-persistence", "--output-format", "json"]
        with tempfile.TemporaryDirectory() as cwd:  # empty dir: no project CLAUDE.md
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                  env=env, cwd=cwd, timeout=900)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            raise RuntimeError(f"claude CLI error: {(proc.stdout or proc.stderr)[:500]}")
        text = (data.get("result") or "").strip()
        if data.get("is_error") or not text:
            if "limit" in text.lower():
                raise _quota(text[:300])
            raise RuntimeError(f"claude CLI error: {text[:500] or proc.stderr[:500]}")
        u = data.get("usage") or {}
        _log_usage(f"cli:{self.model_name} in={u.get('input_tokens')} out={u.get('output_tokens')} "
                   f"cost~${data.get('total_cost_usd', 0):.3f}")
        return _Response(text)

    def generate_content(self, contents, generation_config=None, **_ignored):
        prompt = contents if isinstance(contents, str) else "\n".join(str(c) for c in contents)
        if self.cli:
            return self._generate_cli(prompt)
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
            raise _quota(str(e)) from e
        if response.stop_reason == "refusal":
            raise RuntimeError("Claude declined this request")
        text = "".join(b.text for b in response.content if b.type == "text")
        u = response.usage
        _log_usage(f"{self.model_name} in={u.input_tokens} out={u.output_tokens} stop={response.stop_reason}")
        if not text:
            raise RuntimeError(f"Claude returned no text (stop_reason={response.stop_reason})")
        return _Response(text)
