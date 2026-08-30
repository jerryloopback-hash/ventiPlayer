"""Download Apollo and FlashSR model weights to local cache.

Run this script once to pre-download all model files needed.
After this, models can run offline.

Targets:
  ~/.ventiplayer/models/apollo/
      apollo_model_uni.ckpt   (~70 MB)  Universal lossy enhancer (codec repair)
      config_apollo_uni.yaml            (also bundled in repo; copied here as backup)
  ~/.ventiplayer/models/flashsr/
      student_ldm.pth         (986 MB)  Distilled latent diffusion model
      sr_vocoder.pth          (599 MB)  Super-resolution vocoder
      vae.pth                 (1.6 GB)  Variational autoencoder
"""
import os
from pathlib import Path
from urllib.request import urlretrieve

MODELS_DIR = Path.home() / ".ventiplayer" / "models"
APOLLO_DIR = MODELS_DIR / "apollo"
FLASHSR_DIR = MODELS_DIR / "flashsr"

# Apollo "Universal Lossy Enhancer" — GitHub release assets (deton24 fork)
APOLLO_BASE = ("https://github.com/deton24/"
               "Lew-s-vocal-enhancer-for-Apollo-by-JusperLee/releases/download/uni")
APOLLO_FILES = {
    "apollo_model_uni.ckpt": f"{APOLLO_BASE}/apollo_model_uni.ckpt",
    "config_apollo_uni.yaml": f"{APOLLO_BASE}/config_apollo_uni.yaml",
}

# FlashSR weights — HuggingFace dataset (via hf-mirror for CN access)
HF_MIRROR = os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
FLASHSR_REPO = "datasets/jakeoneijk/FlashSR_weights"
FLASHSR_FILES = ["student_ldm.pth", "sr_vocoder.pth", "vae.pth"]


def _download(url: str, dest: Path):
    if dest.exists() and dest.stat().st_size > 0:
        print(f"  Already exists: {dest.name} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Downloading: {url}")
    try:
        urlretrieve(url, str(dest))
        print(f"  Saved: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
    except Exception as e:
        print(f"  [ERROR] Download failed: {e}")
        print(f"  Manual download: {url}\n  Place at: {dest}")


def main():
    print("=== Downloading audio enhancement model files ===\n")

    print("--- Apollo (codec repair) ---")
    for fname, url in APOLLO_FILES.items():
        _download(url, APOLLO_DIR / fname)

    print("\n--- FlashSR (sample-rate super-resolution) ---")
    print("  (large: ~3.2 GB total)")
    for fname in FLASHSR_FILES:
        url = f"{HF_MIRROR}/{FLASHSR_REPO}/resolve/main/{fname}"
        _download(url, FLASHSR_DIR / fname)

    print("\n=== Done ===")
    print("Models can now run offline.")
    print("Note: the FlashSR model code is vendored in src/models/flashsr_src/,")
    print("      and Apollo's config is also bundled in src/models/apollo_src/configs/.")


if __name__ == "__main__":
    main()
