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
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
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
    # ----- zipped-mode extras (type == "zipped") -----
    type: str = "raw"                       # "raw" or "zipped"
    remote_zips_root: str = ""              # e.g. "/home/abuhafsi/videos_cold_zips"
    local_temp_zips: Path = Path("/tmp")    # cache dir for downloaded zips
    # Tracks which (series, filename) we've fetched in the current process
    # so cleanup_zip_if_series_done() knows what to wipe.
    _fetched_videos: dict[str, set[str]] = field(default_factory=dict)

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
        local_temp_zips_raw = section.get(
            "local_temp_zips", str(Path(tempfile.gettempdir()) / "abuhafs_cold_zips")
        )
        return cls(
            enabled=True,
            ssh_host=section.get("ssh_host", ""),
            remote_root=section.get("ssh_remote_root", "").rstrip("/"),
            local_workspace=local_workspace,
            fetch_method=section.get("fetch_method", "scp"),
            base_url=section.get("base_url", ""),
            ssh_port=int(section.get("ssh_port", 22)),
            ssh_key=section.get("ssh_key", ""),
            type=str(section.get("type", "raw")).strip().lower() or "raw",
            remote_zips_root=section.get("ssh_remote_zips_root", "").rstrip("/"),
            local_temp_zips=Path(local_temp_zips_raw),
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


    # ------------------------------------------------------------------
    # Zipped mode
    # ------------------------------------------------------------------
    def remote_zip_path_for(self, series: str) -> str:
        """Path of the series zip on the cold server."""
        root = (self.remote_zips_root or "").rstrip("/")
        return f"{root}/{series}.zip"

    def local_zip_path_for(self, series: str) -> Path:
        """Local cache path of the series zip on the processing host."""
        return Path(self.local_temp_zips) / f"{series}.zip"

    def _ssh_cmd(self) -> list[str]:
        cmd = ["ssh"]
        if self.ssh_port and self.ssh_port != 22:
            cmd += ["-p", str(self.ssh_port)]
        if self.ssh_key:
            cmd += ["-i", self.ssh_key]
        return cmd

    def _remote_file_exists(self, abs_path: str) -> bool:
        """`test -f` over ssh. Returns False on any error."""
        if not self.ssh_host:
            return False
        cmd = self._ssh_cmd() + [
            self.ssh_host,
            f"test -f {shlex.quote(abs_path)}",
        ]
        try:
            r = subprocess.run(cmd, check=False, capture_output=True, text=True)
        except FileNotFoundError:
            return False
        return r.returncode == 0

    def _fetch_zip_if_needed(self, series: str) -> Path:
        """Make sure `<local_temp_zips>/<series>.zip` exists. Returns the path.

        If the local zip is already present we trust it and return immediately
        — no remote-vs-local size check (cheap-and-cheerful; the upload
        script atomically renames so partial files shouldn't be there).
        """
        local_zip = self.local_zip_path_for(series)
        if local_zip.exists() and local_zip.stat().st_size > 0:
            return local_zip

        if not self.ssh_host or not self.remote_zips_root:
            raise RuntimeError(
                "ColdStorage(zipped) misconfigured: ssh_host / "
                "ssh_remote_zips_root not set"
            )

        remote_zip = self.remote_zip_path_for(series)
        if not self._remote_file_exists(remote_zip):
            raise FileNotFoundError(
                f"cold-storage(zipped): remote zip not found: {remote_zip}"
            )

        local_zip.parent.mkdir(parents=True, exist_ok=True)
        # Stream into a `.part` file then rename so a Ctrl+C / network drop
        # doesn't leave us with a half zip that looks complete.
        part = local_zip.with_suffix(local_zip.suffix + ".part")
        if part.exists():
            try:
                part.unlink()
            except OSError:
                pass

        remote_spec = f"{self.ssh_host}:{_quote_remote(remote_zip)}"
        if self.fetch_method == "rsync":
            cmd = ["rsync", "-a", "--partial", "--inplace"]
            if self.ssh_port and self.ssh_port != 22:
                cmd += ["-e", f"ssh -p {self.ssh_port}"]
            cmd += [remote_spec, str(part)]
        else:
            cmd = ["scp", "-q"] + self._ssh_base_args() + [
                remote_spec, str(part),
            ]

        logger.info("cold-storage(zipped): fetching %s -> %s",
                    remote_zip, local_zip)
        try:
            result = subprocess.run(cmd, check=False, capture_output=True,
                                    text=True)
        except FileNotFoundError as e:
            raise RuntimeError(
                f"cold-storage: '{cmd[0]}' not found on this host."
            ) from e
        if result.returncode != 0:
            try:
                if part.exists():
                    part.unlink()
            except OSError:
                pass
            raise FileNotFoundError(
                f"cold-storage(zipped): failed to fetch {remote_zip} "
                f"(rc={result.returncode}): {(result.stderr or '').strip()}"
            )
        if not part.exists() or part.stat().st_size == 0:
            raise FileNotFoundError(
                f"cold-storage(zipped): empty download for {remote_zip}"
            )
        # Atomic rename onto the final name.
        try:
            if local_zip.exists():
                local_zip.unlink()
        except OSError:
            pass
        part.replace(local_zip)
        return local_zip

    def ensure_local_from_zip(self, series: str, video_filename: str) -> Path:
        """Ensure `<series>/<video_filename>` is locally available.

        Steps:
            1. Compute local target (`<local_workspace>/<series>/<filename>`).
               If it already exists, return it.
            2. Fetch (if needed) the series zip from the cold server.
            3. Extract just the requested video into the local workspace.
            4. Track the fetch so a later
               ``cleanup_zip_if_series_done(series)`` call can clean up.
            5. Return the local path.

        Notes:
            * The lookup inside the zip is name-based (matches the basename),
              so nested folders inside the series are handled transparently.
        """
        if not self.enabled:
            raise RuntimeError("ColdStorage is not enabled")
        if (self.type or "").lower() != "zipped":
            raise RuntimeError(
                "ensure_local_from_zip() called but cold_storage.type != 'zipped'"
            )
        if not series or not video_filename:
            raise ValueError("series and video_filename are required")

        series_dir = Path(self.local_workspace) / series
        local_target = series_dir / video_filename
        if local_target.exists():
            self._fetched_videos.setdefault(series, set()).add(video_filename)
            return local_target

        zip_path = self._fetch_zip_if_needed(series)

        series_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Find a member whose basename matches; case-sensitive on Linux,
            # which is what we want.
            target_member: Optional[zipfile.ZipInfo] = None
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if PurePosixPath(info.filename).name == video_filename:
                    target_member = info
                    break
            if target_member is None:
                raise FileNotFoundError(
                    f"cold-storage(zipped): {video_filename!r} not in "
                    f"{zip_path.name}"
                )
            # Stream the member into local_target so memory stays small even
            # for >2 GB videos.
            tmp = local_target.with_suffix(local_target.suffix + ".part")
            try:
                with zf.open(target_member, "r") as src, tmp.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
            except Exception:
                try:
                    if tmp.exists():
                        tmp.unlink()
                except OSError:
                    pass
                raise
            tmp.replace(local_target)

        self._fetched_videos.setdefault(series, set()).add(video_filename)
        logger.info("cold-storage(zipped): extracted %s/%s -> %s",
                    series, video_filename, local_target)
        return local_target

    def cleanup_zip_if_series_done(self, series: str) -> bool:
        """If no pending videos remain for ``series``, drop the local zip cache
        and any extracted copies.

        Returns True if anything was deleted, False otherwise.

        Safety: we use the DB (or pending.json) to verify that **no** videos
        in this series are still in a non-terminal state. If we can't make
        that determination confidently, we keep the cache (better to use a
        bit of disk than to re-download a 10 GB zip).
        """
        if not series:
            return False
        if (self.type or "").lower() != "zipped":
            return False

        pending_remaining = _count_pending_for_series(series)
        if pending_remaining is None:
            logger.debug(
                "cold-storage(zipped): can't determine pending count for "
                "%r — keeping zip cache", series,
            )
            return False
        if pending_remaining > 0:
            logger.debug(
                "cold-storage(zipped): %d videos still pending for %r — "
                "keeping zip cache", pending_remaining, series,
            )
            return False

        deleted_anything = False
        zip_path = self.local_zip_path_for(series)
        try:
            if zip_path.exists():
                zip_path.unlink()
                deleted_anything = True
                logger.info("cold-storage(zipped): removed cached zip %s",
                            zip_path)
        except OSError as e:
            logger.warning("cold-storage(zipped): couldn't remove %s: %s",
                           zip_path, e)

        # Wipe any extracted videos under <local_workspace>/<series>/.
        series_dir = Path(self.local_workspace) / series
        if series_dir.exists() and series_dir.is_dir():
            try:
                shutil.rmtree(series_dir)
                deleted_anything = True
                logger.info(
                    "cold-storage(zipped): removed extracted dir %s",
                    series_dir,
                )
            except OSError as e:
                logger.warning(
                    "cold-storage(zipped): couldn't remove %s: %s",
                    series_dir, e,
                )

        self._fetched_videos.pop(series, None)
        return deleted_anything


def _count_pending_for_series(series: str) -> Optional[int]:
    """Count videos for ``series`` that are NOT in a terminal state.

    Tries the DB first (canonical), falls back to pending.json. Returns
    ``None`` if we can't read either source. The DB stores series as a
    plain text column.
    """
    series = (series or "").strip()
    if not series:
        return None

    # ----- DB path -----
    try:
        from src.db import Video, get_session_factory  # type: ignore

        Session = get_session_factory()
        with Session() as s:
            rows = s.query(Video).filter(Video.series == series).all()
        if rows:
            remaining = sum(
                1 for r in rows
                if (r.status or "").lower() not in ("completed", "failed")
            )
            return remaining
    except Exception as e:
        logger.debug("cold-storage(zipped): DB pending count failed: %s", e)

    # ----- pending.json fallback -----
    try:
        import json
        from pathlib import Path as _P

        # Heuristic: project root is parent of `src/`.
        root = _P(__file__).resolve().parent.parent
        pj = root / "output" / "pending.json"
        if pj.exists():
            data = json.loads(pj.read_text(encoding="utf-8"))
            remaining = sum(
                1 for d in data
                if (d.get("series") or "").strip() == series
                and (d.get("status") or "").lower() not in ("completed", "failed")
            )
            return remaining
    except Exception as e:
        logger.debug("cold-storage(zipped): JSON pending count failed: %s", e)

    return None


def _quote_remote(path: str) -> str:
    """Quote a path for use after ``host:`` in scp/rsync.

    scp parses ``host:`` then passes the rest to a remote shell, so paths
    with spaces (Arabic series names like "شرح الرسالة") need to be quoted
    for that remote shell. We use single quotes for the remote shell and
    escape any embedded single quotes.
    """
    escaped = path.replace("'", "'\\''")
    return f"'{escaped}'"
