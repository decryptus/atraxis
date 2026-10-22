"""Compatibility entry point; transport is implemented in the client package."""
from atropos_client.worker import fetch, main

if __name__ == "__main__":
    main()
