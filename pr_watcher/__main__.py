"""Entry point: python -m pr_watcher"""
from .config import parse_args
from .app import PRWatcherApp


def main() -> None:
    config = parse_args()
    PRWatcherApp(config).run()


if __name__ == "__main__":
    main()
