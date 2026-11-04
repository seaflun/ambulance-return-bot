from __future__ import annotations

import hashlib
from pathlib import Path
import ssl
import subprocess
import sys


# Public intermediate downloaded from the leaf certificate's official TWCA AIA:
# https://sslserver.twca.com.tw/cacert/Cyber_SSL_2026.crt (2026-11-03).
# This is not a root or a site certificate. Importing it into CA adds no trust anchor.
CERTIFICATE_PATH = Path(__file__).with_name("certificates") / "twca_ssl_2026.cer"
CERTIFICATE_SHA256 = "5775b8011198c498af26961ccbf77dac14a127a221ebc14012ed80f8a20f35f8"


def repair_consumables_certificate_chain() -> bool:
    """Cache the pinned missing issuer; Chrome must still validate the full chain."""
    if sys.platform != "win32":
        return False
    try:
        certificate = ssl.PEM_cert_to_DER_cert(CERTIFICATE_PATH.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise RuntimeError("耗材中繼憑證檔無法讀取，停止登入。") from exc
    if hashlib.sha256(certificate).hexdigest() != CERTIFICATE_SHA256:
        raise RuntimeError("耗材中繼憑證指紋不符，停止登入。")
    try:
        result = subprocess.run(
            ["certutil.exe", "-user", "-addstore", "CA", str(CERTIFICATE_PATH)],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("耗材中繼憑證快取修復失敗，請確認 Windows 憑證設定；尚未送出帳密。") from exc
    if result.returncode != 0:
        raise RuntimeError("耗材中繼憑證無法加入目前使用者的 CA 存放區；尚未送出帳密。")
    return True
