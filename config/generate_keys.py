"""
RSA Key Pair Generator
======================
Run this script to generate your RSA key pair for Tiger Securities authentication.
Upload the PUBLIC key to the Tiger developer portal.
Keep the PRIVATE key secure — never share it.

Usage: python config/generate_keys.py
"""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.backends import default_backend
import os


def generate_rsa_keypair(key_size: int = 2048):
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=key_size,
        backend=default_backend()
    )

    # Private key (PKCS8 PEM format — required by Tiger SDK)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption()
    ).decode()

    # Public key (PEM format — upload this to Tiger developer portal)
    public_pem = private_key.public_key().private_bytes(  # type: ignore
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo  # type: ignore
    ).decode() if hasattr(private_key.public_key(), 'public_bytes') else \
    private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()

    return private_pem, public_pem


if __name__ == "__main__":
    config_dir = os.path.dirname(os.path.abspath(__file__))
    private_path = os.path.join(config_dir, "private_key.pem")
    public_path = os.path.join(config_dir, "public_key.pem")

    if os.path.exists(private_path):
        print(f"Private key already exists at {private_path}")
        overwrite = input("Overwrite? (y/N): ").strip().lower()
        if overwrite != "y":
            print("Aborted.")
            exit(0)

    private_pem, public_pem = generate_rsa_keypair()

    with open(private_path, "w") as f:
        f.write(private_pem)
    os.chmod(private_path, 0o600)  # Restrict permissions

    with open(public_path, "w") as f:
        f.write(public_pem)

    print(f"\nKeys generated successfully!")
    print(f"  Private key: {private_path}  (KEEP SECRET)")
    print(f"  Public key:  {public_path}")
    print("\nNext steps:")
    print("  1. Log in to Tiger developer portal")
    print("  2. Upload the content of public_key.pem")
    print("  3. Copy your Tiger ID and Account number")
    print("  4. Fill in config/settings.py with your credentials")
    print(f"\nPublic key to upload:\n{'-'*40}\n{public_pem}{'-'*40}\n")
