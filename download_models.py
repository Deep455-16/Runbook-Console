import os
from pathlib import Path
from huggingface_hub import snapshot_download, hf_hub_download

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
MODELS_DIR.mkdir(exist_ok=True)

print("Downloading embedding model...")
snapshot_download(
    repo_id="sentence-transformers/all-MiniLM-L6-v2",
    local_dir=str(MODELS_DIR / "MiniLM-L6-v2"),
    local_dir_use_symlinks=False
)

print("Downloading LLM...")
hf_hub_download(
    repo_id="bartowski/Qwen2.5-3B-Instruct-GGUF",
    filename="Qwen2.5-3B-Instruct-Q3_K_M.gguf",
    local_dir=str(MODELS_DIR),
    local_dir_use_symlinks=False
)
print("Download complete.")
