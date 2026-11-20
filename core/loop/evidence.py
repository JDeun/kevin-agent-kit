"""Verify external evidence using public keys pinned by the operator."""

import base64
import hashlib
import json
from pathlib import Path
import subprocess

from .paths import directory, private_temporary_directory, regular_at

OPENSSL = Path("/usr/bin/openssl")
ED25519 = bytes.fromhex("302a300506032b6570032100")


def canonical(payload):
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("ascii")


def public_key(path):
    with directory(path.parent) as parent, regular_at(parent, path.name, single_link=True) as stream:
        lines = stream.read(1025).splitlines()
    if len(lines) < 3 or lines[0] != b"-----BEGIN PUBLIC KEY-----" or lines[-1] != b"-----END PUBLIC KEY-----":
        raise ValueError(f"expected a PEM Ed25519 public key: {path}")
    try:
        der = base64.b64decode(b"".join(lines[1:-1]), validate=True)
    except ValueError:
        raise ValueError(f"invalid Ed25519 public key: {path}") from None
    if not der.startswith(ED25519) or len(der) != len(ED25519) + 32:
        raise ValueError(f"expected an Ed25519 public key: {path}")
    return der


def source_id(key):
    return hashlib.sha256(key).hexdigest()


def signed_receipt(raw, key):
    try:
        envelope = json.loads(raw)
        payload = envelope["payload"]
        signature = base64.b64decode(envelope["signature"], validate=True)
        if not isinstance(payload, dict) or len(signature) != 64:
            raise ValueError
        message = canonical(payload)
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("adapter must return signed evidence") from None
    with private_temporary_directory("kit-evidence-") as root:
        (root / "public.der").write_bytes(key)
        (root / "payload").write_bytes(message)
        (root / "signature").write_bytes(signature)
        result = subprocess.run(
            [str(OPENSSL), "pkeyutl", "-verify", "-pubin", "-keyform", "DER", "-inkey", "public.der",
             "-rawin", "-in", "payload", "-sigfile", "signature"],
            cwd=root, env={"LANG": "C", "OPENSSL_CONF": "/dev/null"}, timeout=10,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if result.returncode != 0:
        raise ValueError("evidence signature does not match the approved source")
    return payload
