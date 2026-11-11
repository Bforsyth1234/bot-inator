"""First-run engine choice (#93).

A new install starts with no engine loaded and asks the user to pick one:
Local (everything stays on the Mac), Cloud key (any #97 preset) or Mixed
(only tool drafts go to the cloud, the #26 per-engine override). The
screen marks one choice "Recommended for this Mac" from physical memory,
but never picks for the user: nothing reaches a cloud engine until the
user has chosen it and pasted their own key (invariant 7c).

DECISIONS (todo #93):

* Pending = no ``~/.bot-inator/settings.json`` yet (``AGENT_SETTINGS_PATH``
  overrides). An install that has one never sees the screen and is never
  changed. ``AGENT_SKIP_FIRST_RUN=1`` (env only) turns the screen off for
  tests and scripted setups.
* While pending the daemon builds :class:`PendingEngine` stand-ins, which
  never load or download anything (decision 5).
* Thresholds: under :data:`CLOUD_BELOW_GB` (24 GB) → Cloud key; 24 GB and
  up → Local. Deterministic; not yet measured on a 16 GB Mac.
* Local has three ways in: a folder of MLX models (checked here), a local
  server (the ``local_server`` preset on a loopback URL), or downloading
  the stock set on a button that states its size.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

SKIP_ENV = "AGENT_SKIP_FIRST_RUN"
# Saved with the choice, so the settings file says how it was set up.
CHOICE_KEY = "AGENT_FIRST_RUN_CHOICE"
CHOICES: tuple[str, ...] = ("local", "cloud", "mixed")
CLOUD_BELOW_GB = 24

# Stock model download sizes, from the Hugging Face cache on 2026-11-11
# (``du -sk`` of each ``models--*`` folder × 1024). 13.7 GB = 12.7 GiB.
STOCK_MODEL_BYTES: dict[str, int] = {
    "mlx-community/Hermes-3-Llama-3.1-8B-4bit": 4_526_702_592,
    "mlx-community/Qwen2.5-1.5B-Instruct-4bit": 880_197_632,
    "mlx-community/Qwen2.5-Coder-14B-Instruct-4bit": 8_321_122_304,
}

NOT_CHOSEN = "Choose an engine first: the first-run screen has not been answered"


# --- memory and the recommendation ------------------------------------------

def physical_memory_gb() -> Optional[float]:
    """This Mac's physical memory in GB (``sysctl hw.memsize``), or None."""
    try:
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
        return int(out) / 1024**3
    except Exception:
        pass
    try:
        return os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE") / 1024**3
    except (ValueError, OSError, AttributeError):
        return None


def recommend(gb: Optional[float]) -> dict[str, str]:
    """``{choice, reason}`` for a Mac with ``gb`` of memory. Never a switch:
    the screen only marks this choice; the user picks."""
    if gb is None:
        return {
            "choice": "local",
            "reason": "This Mac's memory could not be read, so the private choice is suggested.",
        }
    shown = f"{gb:.0f} GB"
    if gb < CLOUD_BELOW_GB:
        return {
            "choice": "cloud",
            "reason": (
                f"This Mac has {shown} of memory; the local models need about 13 GB "
                "of it, so a cloud key will be faster and leave room for your apps."
            ),
        }
    return {
        "choice": "local",
        "reason": (
            f"This Mac has {shown} of memory, enough to run the models on the Mac, "
            "so nothing has to leave it."
        ),
    }


# --- pending -----------------------------------------------------------------

def is_pending() -> bool:
    """True until the user has answered the first-run screen."""
    if os.environ.get(SKIP_ENV, "").strip() == "1":
        return False
    import user_settings  # noqa: WPS433 (top-level daemon module)

    return not user_settings.settings_path().exists()


class PendingEngine:
    """The engine a role has before first run is answered.

    Same surface as :class:`ai.mlx_engine.MLXEngine`, but it loads nothing
    and downloads nothing: generation raises :data:`NOT_CHOSEN` and event
    evaluation returns ``""`` (as an engine failure does).
    """

    def __init__(self, role: str) -> None:
        self.role = role
        self.model_name = ""
        self.generation_lock = threading.Lock()

    loaded = False
    current_model = None
    model = None
    tokenizer = None

    async def load(self) -> None:
        raise RuntimeError(NOT_CHOSEN)

    async def unload(self) -> None:
        return None

    async def swap(self, new_model_name: str) -> None:
        raise RuntimeError(NOT_CHOSEN)

    async def generate(self, prompt: str, max_tokens: int = 512) -> str:
        raise RuntimeError(NOT_CHOSEN)

    def generate_sync(self, prompt: str, max_tokens: int = 512) -> str:
        raise RuntimeError(NOT_CHOSEN)

    def generate_chat_sync(self, *args: Any, **kwargs: Any) -> str:
        raise RuntimeError(NOT_CHOSEN)

    async def evaluate_event(self, *args: Any, **kwargs: Any) -> str:
        return ""


# --- local models ------------------------------------------------------------

def _hf_cache_dir() -> Path:
    from . import egress_log  # noqa: WPS433

    return egress_log._hf_cache_dir()


def cached_bytes(model: str, cache_dir: Optional[Path] = None) -> int:
    """Bytes of ``model`` already in the Hugging Face cache."""
    from . import egress_log  # noqa: WPS433

    return egress_log._repo_bytes(model, cache_dir or _hf_cache_dir())


def is_hub_id(model: str) -> bool:
    """A Hugging Face repo id (``org/name``), not a local folder."""
    return bool(model) and not Path(model).expanduser().exists() and "/" in model \
        and not model.startswith(("/", "~", "."))


def local_download_bytes(models: list[str], cache_dir: Optional[Path] = None) -> dict[str, Any]:
    """What a Local choice on ``models`` would download.

    ``{total, done, unknown}``: ``total`` sums the known sizes
    (:data:`STOCK_MODEL_BYTES`) of the hub models, ``done`` what the cache
    already holds of them, ``unknown`` the hub models with no known size.
    A local folder downloads nothing.
    """
    total = done = 0
    unknown: list[str] = []
    for model in dict.fromkeys(models):
        if not is_hub_id(model):
            continue
        size = STOCK_MODEL_BYTES.get(model)
        have = cached_bytes(model, cache_dir)
        if size is None:
            unknown.append(model)
            continue
        total += size
        done += min(have, size)
    return {"total": total, "done": done, "unknown": unknown}


def is_downloaded(model: str, cache_dir: Optional[Path] = None) -> bool:
    """True when ``model`` needs no download: a local folder, or a hub model
    whose snapshot in the cache has its config and weights."""
    path = Path(model).expanduser()
    if path.is_dir():
        return check_model_folder(str(path)) is None
    snapshots = (cache_dir or _hf_cache_dir()) / ("models--" + model.replace("/", "--")) / "snapshots"
    try:
        return any(check_model_folder(str(s)) is None for s in snapshots.iterdir() if s.is_dir())
    except OSError:
        return False


def check_model_folder(path: str) -> Optional[str]:
    """``None`` when ``path`` is a folder mlx-lm can load (a ``config.json``
    plus ``*.safetensors`` weights), else the reason in one sentence."""
    folder = Path(path or "").expanduser()
    if not path or not folder.is_dir():
        return "That folder does not exist."
    if any(folder.glob("*.gguf")) or any(folder.glob("*/*.gguf")):
        return (
            "That folder holds GGUF models (Ollama, LM Studio), which MLX cannot "
            "load. Choose \"Local server\" instead and point it at Ollama or LM Studio."
        )
    if not (folder / "config.json").is_file():
        return "That folder has no config.json, so it is not an MLX model folder."
    if not any(folder.glob("*.safetensors")):
        return "That folder has no .safetensors weights, so it is not an MLX model folder."
    return None


class Downloader:
    """Downloads the local models, only when the user clicks the button.

    One download at a time, in a background thread; :meth:`state` reports
    progress from the cache size. Each model goes through
    ``egress_log.model_download`` so the privacy ledger records it (#82).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._models: list[str] = []
        self.error: Optional[str] = None
        self.finished = False

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, models: list[str]) -> bool:
        """Start downloading ``models``; False if one is already running."""
        with self._lock:
            if self.running:
                return False
            self._models = [m for m in dict.fromkeys(models) if is_hub_id(m)]
            self.error, self.finished = None, False
            self._thread = threading.Thread(target=self._run, name="first-run-download", daemon=True)
            self._thread.start()
            return True

    def _run(self) -> None:
        from . import egress_log  # noqa: WPS433

        try:
            from huggingface_hub import snapshot_download  # noqa: WPS433

            for model in self._models:
                with egress_log.model_download(model):
                    snapshot_download(model)
            self.finished = True
        except Exception as exc:
            logger.exception("first-run model download failed")
            self.error = f"{type(exc).__name__}: {exc}"[:300]

    def state(self) -> dict[str, Any]:
        return {
            "running": self.running, "finished": self.finished, "error": self.error,
            **local_download_bytes(self._models),
        }


downloader = Downloader()
