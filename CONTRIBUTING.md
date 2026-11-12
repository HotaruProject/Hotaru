# Contributing to Hotaru

We welcome pull requests and issue reports that improve Hotaru while respecting its security architecture.

## Getting started

Hotaru requires Python 3.13 or newer and manages dependencies using `uv`.

1. Fork and clone the repository:
   ```bash
   git clone https://github.com/HotaruProject/Hotaru.git
   cd Hotaru
   ```

2. Synchronize dependencies:
   ```bash
   uv sync
   ```

3. Launch Hotaru in local development mode:
   ```bash
   uv run python -m hotaru
   ```

## Development branch

Send pull requests against the `develop` branch. Direct commits to `main` are reserved for tagged releases.

## Core rules

1. **Security boundary**: Do not weaken capability filters in `relay/` (`caps.py`, `firewall.py`, `sandbox.py`, or `denylist.py`). Any changes affecting the isolation boundary require prior architectural review.
2. **Comment policy**: Hotaru maintains a strict policy of zero `#` comments in Python (`.py`) and module (`.hmod`) files. Self-documenting code with clear variable and function names is required. Verify your diff before committing:
   ```bash
   git diff '*.py' '*.hmod' | grep -E "^\+\s*#"
   ```
   This command must return nothing.
3. **No credentials**: Never log, commit, or transfer vault keys, Telegram session strings, or auth tokens outside the local `sanctuary/` directory.
4. **Localization**: Never hardcode user-visible strings in code. Add translatable strings to `lexicon/en.json` and corresponding language dictionaries.

## Verifying changes locally

Before pushing your branch, run the automated checks locally.

1. **Bytecode compilation**:
   ```bash
   uv run --locked python -m compileall -q hotaru relay
   ```

2. **Strict type checking**:
   Hotaru enforces strict type checking using `basedpyright==1.40.1`. Ensure all targets pass without diagnostics.

   In each of `singularity/backend` and `singularity/frontend`, run `npm ci` followed by `npm test`.
   Verify the frontend bundle with `npm run build` in `singularity/frontend`.

3. **Semgrep security scan**:
   ```bash
   uv run --with 'semgrep==1.179.0' semgrep scan --config p/security-audit --config .semgrep/hotaru-security.yml .
   ```

## Creating .hmod modules

Modules in `constellations/` declare the `HOTARU` dictionary with explicit capability requirements:
- `mt`: MTProto RPC calls via GoyGram
- `state`: persistent key-value state
- `net`: outbound network traffic

Modules operate inside Linux namespaces under user `nobody` and communicate with the core strictly over internal UNIX pipes.
