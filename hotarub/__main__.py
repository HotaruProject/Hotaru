import argparse
import asyncio

from .config import RuntimeConfig
from .runtime import Runtime


def main() -> None:
    parser = argparse.ArgumentParser(prog="hotarub")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    config = RuntimeConfig.from_env()
    if args.check:
        config.validate()
        print("configuration: valid")
        return
    asyncio.run(Runtime(config).run())


if __name__ == "__main__":
    main()
