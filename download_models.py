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

    print("\n--- RIFE (video frame interpolation, torch ROCm) ---")
    print("  (~24 MB per version)")
    _download_rife()

    print("\n=== Done ===")
    print("Models can now run offline.")
    print("Note: the FlashSR model code is vendored in src/models/flashsr_src/,")
    print("      and Apollo's config is also bundled in src/models/apollo_src/configs/.")


# RIFE weights — Practical-RIFE official train_log zips (via hf-mirror for CN access).
# 只取包内 flownet.pkl；模型结构代码 vendor 在 src/models/rife/（inference-only）。
RIFE_REPO = "Bash2X/RIFE-Models"
RIFE_VERSIONS = ["v4_25_lite", "v4_25", "v4_26"]
_RIFE_ZIP = {"v4_25_lite": "RIFE_v4.25.lite.zip", "v4_25": "RIFE_v4.25.zip",
             "v4_26": "RIFE_v4.26.zip"}


def _download_rife():
    import io
    import zipfile
    from urllib.request import urlopen

    for version in RIFE_VERSIONS:
        dest = MODELS_DIR / "rife" / version / "train_log" / "flownet.pkl"
        if dest.exists() and dest.stat().st_size > 0:
            print(f"  Already exists: {version}/flownet.pkl "
                  f"({dest.stat().st_size / 1024 / 1024:.1f} MB)")
            continue
        url = f"{HF_MIRROR}/{RIFE_REPO}/resolve/main/{_RIFE_ZIP[version]}"
        print(f"  Downloading: {url}")
        try:
            with urlopen(url) as resp:
                data = resp.read()
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                member = next(n for n in zf.namelist()
                              if n.endswith("flownet.pkl"))
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(member))
            print(f"  Saved: {dest} ({dest.stat().st_size / 1024 / 1024:.1f} MB)")
        except Exception as e:
            print(f"  [ERROR] Download failed: {e}")
            print(f"  Manual download: {url}\n  Place at: {dest}")


if __name__ == "__main__":
    main()
