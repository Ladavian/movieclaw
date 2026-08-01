#!/usr/bin/env bash
# =============================================================================
# NER 模型 Release 的更新清单生成（docs/design/in-app-update.md M4）
#
# 应用内模型更新要求模型 Release（tag 形如 torrent-ner-vN）携带 manifest.json
# 供下载后做 sha256 强制校验。发布新模型时：
#   ./scripts/build-model-manifest.sh <模型目录>
# 会在模型目录里生成 manifest.json，与三个模型文件一起上传为 Release assets。
# =============================================================================
set -euo pipefail

MODEL_DIR="${1:?用法：build-model-manifest.sh <含 model.int8.onnx/tokenizer.json/labels.json 的目录>}"

MODEL_DIR="$MODEL_DIR" python3 - <<'PY'
import hashlib
import json
import os
import sys
from pathlib import Path

model_dir = Path(os.environ["MODEL_DIR"])
files = {}
for name in ("model.int8.onnx", "tokenizer.json", "labels.json"):
    path = model_dir / name
    if not path.is_file():
        sys.exit(f"错误：{path} 不存在")
    files[name] = {
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "size": path.stat().st_size,
    }
manifest = {"schema": 1, "files": files}
out = model_dir / "manifest.json"
out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(f"已生成 {out}")
PY

# 可选签名（与 build-release-artifacts.sh 同一机制）：manifest.json.sig 一并上传
if [[ -n "${RELEASE_SIGNING_KEY:-}" ]]; then
    MODEL_DIR="$MODEL_DIR" python3 - <<'PY'
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key

key = load_pem_private_key(os.environ["RELEASE_SIGNING_KEY"].encode(), password=None)
model_dir = Path(os.environ["MODEL_DIR"])
raw = (model_dir / "manifest.json").read_bytes()
(model_dir / "manifest.json.sig").write_bytes(base64.b64encode(key.sign(raw)) + b"\n")
print(f"已生成 {model_dir / 'manifest.json.sig'}")
PY
fi
