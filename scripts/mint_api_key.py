"""Generate an API key and print the entry to add to `API_KEYS` / `API_KEYS_FILE`.

Usage:
    python scripts/mint_api_key.py --source kenya-scraper --countries kenya
    python scripts/mint_api_key.py --source admin --admin

The key is printed once: give it to the source and keep only the entry in the configuration.
"""

import argparse
import hashlib
import json
import secrets


def main() -> None:
    """Print a new key and its configuration entry."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", required=True, help="The source that will own the postings sent with this key.")
    parser.add_argument("--countries", nargs="*", default=[], help="Countries the key may post to.")
    parser.add_argument("--admin", action="store_true", help="Read every source's postings; cannot post.")
    args = parser.parse_args()

    key = secrets.token_urlsafe(32)
    entry = {"source": args.source, "countries": args.countries}
    if args.admin:
        entry["admin"] = True
    key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

    print(f"API key (give it to the source, it is not stored anywhere):\n  {key}\n")
    print("Entry to add to API_KEYS or API_KEYS_FILE:")
    print(json.dumps({key_hash: entry}, indent=2))


if __name__ == "__main__":
    main()
