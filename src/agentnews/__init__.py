from .config import Settings, get_settings
from .db import initialize_database


def main(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    initialize_database(settings.sqlite_path)
    print("AgentNews command-line scaffold is ready.")
