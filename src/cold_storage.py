"""
Cold storage module — fetches video files from a remote SSH server on demand.

Architecture:
    The processing host (e.g. 7erfa-system.com / a small VPS) is not big enough
    to hold the entire backlog of videos. The full collection lives on a
    "cold storage" SSH server (e.g. abuhafs.info / 213.239.209.167). When the
    pipeline needs to process a video it:

        1. Checks if it is already present locally (item.original_path).
        2. If not, scp/rsync downloads it from the cold server into the
           local workspace (cfg.paths['videos_input']).
        3. Processes it normally (thumbnail, shorts cutting, FB upload, ...).
        4. Cleans up the local copy so disk usage stays bounded (only ~1 video
           on disk at a time).

Usage:
    fetcher = ColdStorage.from_config(cfg)
    if fetcher.enabled and not fetcher.is_local(item.original_path):
        local_path = fetcher.ensure_local(item.original_path)
    # ... use local_path ...
    fetcher.cleanup(local_path)  # delete after processing
"""
from __future__ import annotations

import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Optional


logger = logging.getLogger(__name__)


# Markers used to identify the start of the "series-relative" portion of a path.
# Any path containing one of these segments will have everything *after* it
# treated as the relative path on the cold server.
# The order matters: we check the more specific markers first.
_PATH_MARKERS = (
    "videos_workspace",
    "videos_cold",
    "videos_input",
    "مرئيات",
)


@dataclass
class ColdStorage:
    enabled: bool
    ssh_host: str           # e.g. "abuhafsi@213.239.209.167"
    remote_root: str        # e.g. "/home/abuhafsi/videos_cold"
    local_workspace: Path   # e.g. /opt/abuhafs/videos_workspace/
    fetch_method: str = "scp"   # "scp" or "rsync"
    base_url: str = ""          # optional HTTP fallback
    ssh_port: int = 22
    ssh_key: str = ""           # optional path to private key

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------
    @classmethod
    def from_config(cls, cfg) -> "ColdStorage":
        """Read settings from cfg.cold_storage.

        If disabled or the section is missing, returns an instance with
        enabled=False and no-op behavior.
        """
        section = cfg.get("cold_storage", default={}) or {}
        if not section.get("enabled", False):
            return cls(
                enabled=False,
                ssh_host="",
                remote_root="",
                local_workspace=Path("."),
            )
        try:
            local_workspace = Path(cfg.paths["videos_input"])
        except Exception:
            local_workspace = Path(".")
        return cls(
            enabled=True,
            ssh_host=section.get("ssh_host", ""),
            remote_root=section.get("ssh_remote_root", "").rstrip("/"),
            local_workspace=local_workspace,
            fetch_method=section.get("fetch_method", "scp"),
            base_url=section.get("base_url", ""),
            ssh_port=int(section.get("ssh_port", 22)),
            ssh_key=section.get("ssh_key", ""),
        )

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------
    def is_local(self, video_path: str | Path) -> bool:
        """Check if the video file exists locally on disk."""
        try:
            return Path(video_path).exists()
        except OSError:
            return False

    def _relative_for(self, original_path: str) -> str:
        """Return the series-relative path (e.g. "شرح الرسالة/1.wmv").

        Strategy: take everything *after* one of the known markers
        ("مرئيات", "videos_workspace", "videos_input", "videos_cold") in the
        path. If no marker is found, fall back to the last two components
        (series/file).
        """
        # Normalise Windows separators to POSIX so we have one shape to work
        # with (the cold server is Linux).
        normalised = str(original_path).replace("\\", "/")
        # Drop any drive letter like "E:".
        if len(normalised) >= 2 and normalised[1] == ":":
            normalised = normalised[2:]
        parts = [p for p in normalised.split("/") if p]

        # Find the LAST occurrence of any marker (so nested "videos_input/..."
        # paths still resolve correctly).
        marker_index = -1
        for i, p in enumerate(parts):
            if p in _PATH_MARKERS:
                marker_index = i
        if marker_index >= 0 and marker_index + 1 < len(parts):
            rel_parts = parts[marker_index + 1:]
        elif len(parts) >= 2:
            # Fallback: assume "<...>/series/file.ext"
            rel_parts = parts[-2:]
        else:
            rel_parts = parts
        return "/".join(rel_parts)

    def remote_path_for(self, original_path: str) -> str:
        """Map an original_path to the absolute path on the cold server.

        Examples:
            "E:\\فضيلة الشيخ أبي حفص\\مرئيات\\شرح الرسالة\\1.wmv"
              → "/home/abuhafsi/videos_cold/شرح الرسالة/1.wmv"
            "/opt/abuhafs/videos_workspace/شرح الرسالة/1.wmv"
              → "/home/abuhafsi/videos_cold/شرح الرسالة/1.wmv"
        """
        rel = self._relative_for(original_path)
        root = (self.remote_root or "").rstrip("/")
        if not rel:
            return root
        return f"{root}/{rel}"

    def local_path_for(self, original_path: str) -> Path:
        """Path the file will live at locally after fetch."""
        rel = self._relative_for(original_path)
        return self.local_workspace / rel

    # ------------------------------------------------------------------
    # Fetch / cleanup
    # ------------------------------------------------------------------
    def _ssh_base_args(self) -> list[str]:
        args: list[str] = []
        if self.ssh_port and self.ssh_port != 22:
            args += ["-P", str(self.ssh_port)] if self.fetch_method == "scp" else ["-p", str(self.ssh_port)]
        if self.ssh_key:
            args += ["-i", self.ssh_key]
        return args

    def ensure_local(self, original_path: str) -> Path:
        """Ensure the file is available locally. Returns the local path.

        - If already local: returns Path(original_path) directly.
        - Otherwise: fetches from the cold server via scp (default) or rsync
          into ``local_workspace`` and returns the new local path.

        Raises:
            FileNotFoundError: if the remote file doesn't exist or the fetch
                fails.
            RuntimeError: if cold storage is not enabled.
        """
        if not self.enabled:
            raise RuntimeError("ColdStorage is not enabled")

        # Fast path: already on disk.
        if self.is_local(original_path):
            return Path(original_path)

        if not self.ssh_host or not self.remote_root:
            raise RuntimeError(
                "ColdStorage misconfigured: ssh_host / remote_root not set"
            )

        remote_abs = self.remote_path_for(original_path)
        local_target = self.local_path_for(original_path)
        local_target.parent.mkdir(parents=True, exist_ok=True)

        remote_spec = f"{self.ssh_host}:{_quote_remote(remote_abs)}"

        if self.fetch_method == "rsync":
            cmd = ["rsync", "-az", "--partial", "--inplace"]
            if self.ssh_port and self.ssh_port != 22:
                cmd += ["-e", f"ssh -p {self.ssh_port}"]
            cmd += [remote_spec, str(local_target)]
        else:  # scp
            cmd = ["scp", "-q"] + self._ssh_base_args() + [remote_spec, str(local_target)]

        logger.info("cold-storage: fetching %s -> %s", remote_abs, local_target)
        try:
            result = subprocess.run(
                cmd,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as e:
            raise RuntimeError(
                f"cold-storage: '{cmd[0]}' not found on this host. "
                f"Install OpenSSH client (or rsync) first."
            ) from e

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            # Clean up partial file so we don't leave a half-download lying
            # around (especially important since disk is tight on the
            # processing host).
            try:
                if local_target.exists() and local_target.stat().st_size == 0:
                    local_target.unlink()
            except OSError:
                pass
            raise FileNotFoundError(
                f"cold-storage: failed to fetch {remote_abs} (rc={result.returncode}): {stderr}"
            )

        if not local_target.exists():
            raise FileNotFoundError(
                f"cold-storage: fetch reported success but file missing at {local_target}"
            )
        return local_target

    def cleanup(self, local_path: str | Path) -> bool:
        """Delete the local copy after processing.

        Returns True if a file was deleted, False if it wasn't there or
        deletion failed. Also tries to prune empty parent directories
        (series folder) so the workspace stays tidy.
        """
        p = Path(local_path)
        deleted = False
        try:
            if p.exists() and p.is_file():
                p.unlink()
                deleted = True
        except OSError as e:
            logger.warning("cold-storage cleanup: couldn't delete %s: %s", p, e)
            return False

        # Prune empty parent directories *up to but not including* the
        # workspace root, to keep the workspace tidy.
        try:
            workspace = self.local_workspace.resolve()
        except OSError:
            workspace = self.local_workspace
        parent = p.parent
        for _ in range(4):  # cap depth so we never recurse forever
            try:
                resolved = parent.resolve()
            except OSError:
                break
            if not parent.exists() or resolved == workspace:
                break
            try:
                parent.rmdir()  # only succeeds if empty
            except OSError:
                break
            parent = parent.parent
        return deleted

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------
    def list_remote_videos(self, series: str = "") -> list[str]:
        """List videos on the cold server. Returns relative paths (relative
        to ``remote_root``), e.g. ["شرح الرسالة/1.wmv", ...].

        Runs ``ssh <host> 'find <root>/<series> -type f -iname "*.mp4" ...'``.
        Returns an empty list if disabled, on error, or if there are no
        matches.
        """
        if not self.enabled or not self.ssh_host or not self.remote_root:
            return []

        sub = (series or "").strip().strip("/")
        if sub:
            remote_dir = f"{self.remote_root}/{sub}"
        else:
            remote_dir = self.remote_root

        # iname filters for common video extensions.
        exts = ("mp4", "mkv", "avi", "wmv", "flv", "mov", "m4v", "webm")
        find_filters = " -o ".join(f"-iname '*.{e}'" for e in exts)
        remote_cmd = (
            f"find {shlex.quote(remote_dir)} -type f \\( {find_filters} \\) 2>/dev/null"
        )

        ssh_cmd = ["ssh"]
        if self.ssh_port and self.ssh_port != 22:
            ssh_cmd += ["-p", str(self.ssh_port)]
        if self.ssh_key:
            ssh_cmd += ["-i", self.ssh_key]
        ssh_cmd += [self.ssh_host, remote_cmd]

        try:
            result = subprocess.run(
                ssh_cmd,
                check=False,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            logger.warning("cold-storage: ssh not found on PATH")
            return []
        if result.returncode != 0:
            logger.warning(
                "cold-storage list_remote_videos failed (rc=%s): %s",
                result.returncode, (result.stderr or "").strip(),
            )
            return []

        rels: list[str] = []
        root_pp = PurePosixPath(self.remote_root)
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rel = str(PurePosixPath(line).relative_to(root_pp))
            except ValueError:
                rel = line
            rels.append(rel)
        return rels

    def list_remote_series(self) -> list[str]:
        """List top-level series directories on the cold server."""
        if not self.enabled or not self.ssh_host or not self.remote_root:
            return []
        remote_cmd = (
            f"find {shlex.quote(self.remote_root)} -mindepth 1 -maxdepth 1 "
            f"-type d -printf '%f\\n' 2>/dev/null"
        )
        ssh_cmd = ["ssh"]
        if self.ssh_port and self.ssh_port != 22:
            ssh_cmd += ["-p", str(self.ssh_port)]
        if self.ssh_key:
            ssh_cmd += ["-i", self.ssh_key]
        ssh_cmd += [self.ssh_host, remote_cmd]
        try:
            result = subprocess.run(
                ssh_cmd, check=False, capture_output=True, text=True,
            )
        except FileNotFoundError:
            return []
        if result.returncode != 0:
            return []
        return [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]


def _quote_remote(path: str) -> str:
    """Quote a path for use after ``host:`` in scp/rsync.

    scp parses ``host:`` then passes the rest to a remote shell, so paths
    with spaces (Arabic series names like "شرح الرسالة") need to be quoted
    for that remote shell. We use single quotes for the remote shell and
    escape any embedded single quotes.
    """
    escaped = path.replace("'", "'\\''")
    return f"'{escaped}'"
