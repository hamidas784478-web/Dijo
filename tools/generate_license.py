"""
Dijo license code generator — for YOUR use only, never ship this to customers.

Generates offline-verifiable license codes for the Pro features (AI chat,
Excel/CSV export, backup/restore). No server or database of codes needed —
the app verifies codes locally using the same secret this script uses.

IMPORTANT: set DIJO_LICENSE_SECRET to the same value in both:
  - wherever you run this script
  - the app.py you distribute (as an env var, or by editing the
    LICENSE_SECRET default directly before distributing)
If they don't match, codes generated here won't validate in the app.

Usage:
    export DIJO_LICENSE_SECRET="your-own-long-random-secret"
    python3 generate_license.py            # generates one PRO code
    python3 generate_license.py PRO 5      # generates 5 PRO codes
"""
import os
import sys
import secrets
import hmac
import hashlib

LICENSE_SECRET = os.environ.get("DIJO_LICENSE_SECRET", "dijo-change-this-license-secret-before-selling")


def generate_license_code(plan="PRO", secret=None):
    secret = secret or LICENSE_SECRET
    nonce = secrets.token_hex(4).upper()
    sig = hmac.new(secret.encode(), f"{plan}:{nonce}".encode(), hashlib.sha256).hexdigest()[:12].upper()
    return f"DIJO-{plan}-{nonce}-{sig}"


if __name__ == "__main__":
    plan = sys.argv[1] if len(sys.argv) > 1 else "PRO"
    count = int(sys.argv[2]) if len(sys.argv) > 2 else 1

    if LICENSE_SECRET == "dijo-change-this-license-secret-before-selling":
        print("⚠️  Warning: you're using the DEFAULT secret. Anyone with the app's")
        print("   source code can generate free codes. Set DIJO_LICENSE_SECRET to")
        print("   your own value in both this script's environment and the app's,")
        print("   before selling any codes.\n")

    for _ in range(count):
        print(generate_license_code(plan))
