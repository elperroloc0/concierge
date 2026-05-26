#!/usr/bin/env python
"""
Generate VAPID key pair for web push notifications.

Output format matches py-vapid >= 1.9.x:
    - public key  → base64url of uncompressed point (65 bytes)
    - private key → base64url of raw 32-byte EC private value

Run once per environment. Copy output into .env / Render env vars.
Never commit VAPID_PRIVATE_KEY into git.

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

    # Public key — uncompressed (0x04 || X || Y), 65 bytes — base64url
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )

    # Private key — raw 32-byte EC private value — base64url
    priv_raw = priv.private_numbers().private_value.to_bytes(32, "big")

    print("# Copy these into your .env (and Render env vars for production):")
    print()
    print(f"VAPID_PUBLIC_KEY={_b64url(pub_bytes)}")
    print(f"VAPID_PRIVATE_KEY={_b64url(priv_raw)}")
    print("VAPID_ADMIN_EMAIL=admin@your-domain.com")


if __name__ == "__main__":
    main()
