import os

from .storage import Store

if __name__ == "__main__":
    Store(os.environ["DATABASE_URL"]).migrate()
