#!/usr/bin/env bash
# =============================================================================
# 更新清单签名密钥对生成（docs/design/in-app-update.md 二期加固）
#
# 用法：./scripts/gen-release-signing-key.sh [输出目录，缺省当前目录]
#
# 产出与使用：
#   release-signing-key.pem  —— Ed25519 私钥。⚠️ 绝不入库！配置为仓库的
#                               GitHub Actions 机密 RELEASE_SIGNING_KEY
#                               （内容整段粘贴），发版时自动生成签名。
#   打印的公钥 base64        —— 配置到镜像/部署的 UPDATE_MANIFEST_PUBKEY
#                               环境变量（或 docker build --build-arg），
#                               配置后应用内更新强制验签。
# =============================================================================
set -euo pipefail

OUT_DIR="${1:-.}"
mkdir -p "$OUT_DIR"
KEY_FILE="$OUT_DIR/release-signing-key.pem"

if [[ -e "$KEY_FILE" ]]; then
    echo "错误：$KEY_FILE 已存在，拒绝覆盖（如确要更换密钥请先自行移走旧密钥）" >&2
    exit 1
fi

KEY_FILE="$KEY_FILE" python3 - <<'PY'
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

key = Ed25519PrivateKey.generate()
key_file = Path(os.environ["KEY_FILE"])
key_file.write_bytes(
    key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
)
key_file.chmod(0o600)
pub_b64 = base64.b64encode(
    key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
).decode()
print(f"私钥已写入：{key_file}（权限 600，请配置为 CI 机密 RELEASE_SIGNING_KEY 后妥善保管）")
print(f"公钥（配置为 UPDATE_MANIFEST_PUBKEY）：{pub_b64}")
PY
