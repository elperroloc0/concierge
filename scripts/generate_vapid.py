#!/usr/bin/env python
"""
Generate VAPID key pair for web push notifications.

Run once per environment. Output goes to stdout — copy values into .env / Render env vars.
Never check VAPID_PRIVATE_KEY into git.

Usage:
    python scripts/generate_vapid.py
"""
import base64

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> None:
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub  = priv.public_key()

    # Public key — uncompressed (0x04 || X || Y), 65 bytes — base64url for browser
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    # Private key — PEM, used server-side by pywebpush
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    print("# Copy these into your .env (and Render env vars for production):")
    print()
    print(f"VAPID_PUBLIC_KEY={_b64url(pub_bytes)}")
    print()
    print("VAPID_PRIVATE_KEY=\"" + priv_pem.replace("\n", "\\n") + "\"")
    print()
    print("VAPID_ADMIN_EMAIL=admin@your-domain.com")


if __name__ == "__main__":
    main()
