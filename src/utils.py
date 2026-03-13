import json
import yaml
from pathlib import Path
from typing import Any, Dict


def load_context_config(env_name: str, config_dir: str = "config") -> Dict[str, Any]:
    """
    Load AWS CDK context configuration from a YAML or JSON file.

    Supports:
      - .yaml/.yml OR .json (but not both)
      - base.yaml merged with environment config
      - Only 'dev', 'stage', or 'prod' environments are valid

    Raises:
      - ValueError if environment is invalid or multiple config files exist
      - FileNotFoundError if expected config file not found
    """

    # ✅ Validate environment name
    VALID_ENVS = {"dev", "stage", "prod"}
    if env_name not in VALID_ENVS:
        raise ValueError(
            f"Invalid environment '{env_name}'. "
            f"Must be one of: {', '.join(sorted(VALID_ENVS))}"
        )

    # Define possible config paths
    base_path = Path(config_dir) / "base.yaml"
    env_yaml = Path(config_dir) / f"{env_name}.yaml"
    env_yml = Path(config_dir) / f"{env_name}.yml"
    env_json = Path(config_dir) / f"{env_name}.json"

    def read_file(path: Path) -> Dict[str, Any]:
        """Read YAML or JSON file into a dictionary."""
        with open(path, "r") as f:
            if path.suffix in (".yaml", ".yml"):
                return yaml.safe_load(f) or {}
            elif path.suffix == ".json":
                return json.load(f)
            else:
                raise ValueError(f"Unsupported config file type: {path.suffix}")

    # Load base config (optional)
    base_config = {}
    if base_path.exists():
        base_config = read_file(base_path)

    # Detect existing env-specific file(s)
    env_files = [p for p in [env_yaml, env_yml, env_json] if p.exists()]

    if not env_files:
        raise FileNotFoundError(
            f"No config file found for environment '{env_name}' "
            f"in {config_dir}. Expected one of: {env_yaml}, {env_yml}, {env_json}"
        )

    if len(env_files) > 1:
        raise ValueError(
            f"Multiple config files found for environment '{env_name}': {env_files}. "
            f"Use only one (.yaml/.yml OR .json)."
        )

    # Load and merge configs
    env_config = read_file(env_files[0])
    merged_config = {**base_config, **env_config}

    return merged_config
