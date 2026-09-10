import argparse
import asyncio
import importlib.util
import sys


def ensure_kernel_dependencies() -> None:
    spec = importlib.util.spec_from_file_location("hotaru_deps_bootstrap", __file__.replace("__main__.py", "deps.py"))
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return
    from pathlib import Path
    import tomllib
    try:
        project = tomllib.loads(Path(__file__).replace("__main__.py", "../pyproject.toml").resolve().read_text(encoding="utf-8"))
        requirements = list(project.get("project", {}).get("dependencies", []))
    except Exception:
        requirements = ["goygram>=0.7.78"]
    if not requirements:
        return
    module_root = module
    satisfied = True
    for line in requirements:
        try:
            requirement = module_root.parse_requirement(line)
        except module_root.DependencyError:
            satisfied = False
            break
        current = module_root.installed_version(requirement.canonical)
        if current is None or not module_root.requirement_satisfied(requirement, current):
            satisfied = False
            break
    if satisfied:
        return
    try:
        result = asyncio.run(module_root.ensure(requirements))
    except module_root.DependencyError as exc:
        print(f"dependency error: {exc}", file=sys.stderr)
        raise SystemExit(1)
    if result.get("installed") or result.get("upgraded"):
        detail = ", ".join(result["installed"] + result["upgraded"])
        print(f"dependencies resolved via {result['manager']}: {detail}")
        for entry in list(sys.modules):
            if entry == "goygram" or entry.startswith("goygram."):
                del sys.modules[entry]


def main() -> None:
    parser = argparse.ArgumentParser(prog="hotaru")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    ensure_kernel_dependencies()
    from .config import RuntimeConfig
    from .runtime import Runtime
    config = RuntimeConfig.from_database()
    if args.check:
        config.validate()
        print("configuration: valid")
        return
    asyncio.run(Runtime(config).run())


if __name__ == "__main__":
    main()
