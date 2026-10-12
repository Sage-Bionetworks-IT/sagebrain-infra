import json
import pytest
import tempfile
import yaml
from pathlib import Path

from src.utils import load_context_config


class TestLoadContextConfig:
    """Test suite for the load_context_config function."""

    @pytest.mark.parametrize(
        "invalid_env", ["invalid", "test", "production", "development", ""]
    )
    def test_invalid_environment_raises_value_error(self, invalid_env):
        """Test that invalid environment names raise ValueError."""
        with pytest.raises(ValueError, match=f"Invalid environment '{invalid_env}'"):
            load_context_config(invalid_env)

    @pytest.mark.parametrize("valid_env", ["dev", "stage", "prod"])
    def test_valid_environments(self, valid_env):
        """Test that valid environment names are accepted."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / f"{valid_env}.yaml"
            config_data = {
                "FQDN": f"{valid_env}.example.com",
                "VPC_CIDR": "10.0.0.0/16",
                "TAGS": {"Environment": valid_env},
            }

            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            result = load_context_config(valid_env, temp_dir)
            assert result["FQDN"] == f"{valid_env}.example.com"
            assert result["TAGS"]["Environment"] == valid_env

    def test_no_config_file_raises_file_not_found_error(self):
        """Test that missing config files raise FileNotFoundError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            with pytest.raises(
                FileNotFoundError, match="No config file found for environment 'dev'"
            ):
                load_context_config("dev", temp_dir)

    @pytest.mark.parametrize(
        "file_extensions", [(".yaml", ".json"), (".yaml", ".yml"), (".yml", ".json")]
    )
    def test_multiple_config_files_raises_value_error(self, file_extensions):
        """Test that multiple config files for same environment raise ValueError."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ext1, ext2 = file_extensions
            file1 = Path(temp_dir) / f"dev{ext1}"
            file2 = Path(temp_dir) / f"dev{ext2}"

            # Create content based on file extension
            if ext1 == ".json":
                with open(file1, "w") as f:
                    json.dump({"test": "file1"}, f)
            else:
                with open(file1, "w") as f:
                    yaml.dump({"test": "file1"}, f)

            if ext2 == ".json":
                with open(file2, "w") as f:
                    json.dump({"test": "file2"}, f)
            else:
                with open(file2, "w") as f:
                    yaml.dump({"test": "file2"}, f)

            with pytest.raises(
                ValueError, match="Multiple config files found for environment 'dev'"
            ):
                load_context_config("dev", temp_dir)

    @pytest.mark.parametrize(
        "file_extension,config_data",
        [
            (
                ".yaml",
                {
                    "VPC_CIDR": "10.0.0.0/16",
                    "FQDN": "example.com",
                    "TAGS": {"Environment": "dev"},
                },
            ),
            (".yml", {"VPC_CIDR": "10.0.0.0/16", "FQDN": "example.com"}),
            (
                ".json",
                {
                    "VPC_CIDR": "10.0.0.0/16",
                    "FQDN": "example.com",
                    "TAGS": {"Environment": "dev"},
                },
            ),
        ],
    )
    def test_load_config_files(self, file_extension, config_data):
        """Test loading configuration files in different formats."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / f"dev{file_extension}"

            if file_extension == ".json":
                with open(config_file, "w") as f:
                    json.dump(config_data, f)
            else:
                with open(config_file, "w") as f:
                    yaml.dump(config_data, f)

            result = load_context_config("dev", temp_dir)
            assert result == config_data

    def test_base_config_merging(self):
        """Test that base.yaml is properly merged with environment config."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create base config
            base_file = Path(temp_dir) / "base.yaml"
            base_config = {
                "VPC_CIDR": "10.0.0.0/16",
                "COMMON_SETTING": "base_value",
                "SHARED_SETTING": "from_base",
            }
            with open(base_file, "w") as f:
                yaml.dump(base_config, f)

            # Create environment config
            env_file = Path(temp_dir) / "dev.yaml"
            env_config = {
                "FQDN": "dev.example.com",
                "SHARED_SETTING": "from_env",  # This should override base
                "ENV_SPECIFIC": "dev_value",
            }
            with open(env_file, "w") as f:
                yaml.dump(env_config, f)

            result = load_context_config("dev", temp_dir)

            expected = {
                "VPC_CIDR": "10.0.0.0/16",  # from base
                "COMMON_SETTING": "base_value",  # from base
                "SHARED_SETTING": "from_env",  # env overrides base
                "FQDN": "dev.example.com",  # from env
                "ENV_SPECIFIC": "dev_value",  # from env
            }

            assert result == expected

    def test_no_base_config(self):
        """Test loading config when base.yaml doesn't exist."""
        with tempfile.TemporaryDirectory() as temp_dir:
            env_file = Path(temp_dir) / "dev.yaml"
            env_config = {"FQDN": "dev.example.com"}

            with open(env_file, "w") as f:
                yaml.dump(env_config, f)

            result = load_context_config("dev", temp_dir)
            assert result == env_config

    def test_empty_base_config(self):
        """Test handling of empty base config file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create empty base config
            base_file = Path(temp_dir) / "base.yaml"
            with open(base_file, "w") as f:
                f.write("")  # Empty file

            # Create environment config
            env_file = Path(temp_dir) / "dev.yaml"
            env_config = {"FQDN": "dev.example.com"}
            with open(env_file, "w") as f:
                yaml.dump(env_config, f)

            result = load_context_config("dev", temp_dir)
            assert result == env_config

    def test_empty_env_config(self):
        """Test handling of empty environment config file."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create base config
            base_file = Path(temp_dir) / "base.yaml"
            base_config = {"VPC_CIDR": "10.0.0.0/16"}
            with open(base_file, "w") as f:
                yaml.dump(base_config, f)

            # Create empty environment config
            env_file = Path(temp_dir) / "dev.yaml"
            with open(env_file, "w") as f:
                f.write("")  # Empty file

            result = load_context_config("dev", temp_dir)
            assert result == base_config

    def test_custom_config_dir(self):
        """Test using a custom config directory."""
        with tempfile.TemporaryDirectory() as temp_dir:
            custom_dir = Path(temp_dir) / "custom_config"
            custom_dir.mkdir()

            config_file = custom_dir / "dev.yaml"
            config_data = {"FQDN": "custom.example.com"}

            with open(config_file, "w") as f:
                yaml.dump(config_data, f)

            result = load_context_config("dev", str(custom_dir))
            assert result == config_data

    @pytest.mark.parametrize(
        "file_extension,invalid_content,expected_exception",
        [
            (".yaml", "invalid: yaml: content: [unclosed", yaml.YAMLError),
            (".yml", "invalid: yaml: content: [unclosed", yaml.YAMLError),
            (".json", '{"invalid": json content}', json.JSONDecodeError),
        ],
    )
    def test_invalid_config_content(
        self, file_extension, invalid_content, expected_exception
    ):
        """Test handling of invalid configuration file content."""
        with tempfile.TemporaryDirectory() as temp_dir:
            config_file = Path(temp_dir) / f"dev{file_extension}"
            with open(config_file, "w") as f:
                f.write(invalid_content)

            with pytest.raises(expected_exception):
                load_context_config("dev", temp_dir)


class TestSectionMerge:
    """Shallow top-level merge, except allow-listed sections (API_APP) which deep-merge."""

    def _load(self, base: dict, env: dict) -> dict:
        with tempfile.TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "base.yaml").write_text(yaml.dump(base))
            (Path(temp_dir) / "dev.yaml").write_text(yaml.dump(env))
            return load_context_config("dev", temp_dir)

    def test_neptune_section_still_replaced_wholesale(self):
        """Existing behaviour that dev.yaml relies on — must not silently change."""
        result = self._load(
            {"NEPTUNE": {"engine_version": "1.3.2.1", "iam_auth_enabled": True}},
            {"NEPTUNE": {"serverless_max_capacity": 4.0}},
        )
        assert result["NEPTUNE"] == {"serverless_max_capacity": 4.0}

    def test_api_app_section_deep_merged(self):
        result = self._load(
            {
                "API_APP": {
                    "enabled": False,
                    "cpu": 512,
                    "rate_limits": {"global_rate": 50, "global_burst": 100},
                }
            },
            {"API_APP": {"enabled": True, "rate_limits": {"global_rate": 25}}},
        )
        assert result["API_APP"] == {
            "enabled": True,
            "cpu": 512,
            "rate_limits": {"global_rate": 25, "global_burst": 100},
        }

    def test_api_app_only_in_base(self):
        result = self._load({"API_APP": {"enabled": False}}, {"OTHER": 1})
        assert result["API_APP"] == {"enabled": False}

    def test_api_app_only_in_env(self):
        result = self._load({"OTHER": 1}, {"API_APP": {"enabled": True}})
        assert result["API_APP"] == {"enabled": True}

    def test_real_configs_unchanged_for_existing_sections(self):
        """Loading the committed configs gives the same NEPTUNE/VIZ/AUTH as a plain shallow merge."""
        config_dir = Path(__file__).parents[2] / "config"
        base = yaml.safe_load((config_dir / "base.yaml").read_text())
        for env in ("dev", "stage", "prod"):
            env_cfg = yaml.safe_load((config_dir / f"{env}.yaml").read_text()) or {}
            shallow = {**base, **env_cfg}
            result = load_context_config(env, str(config_dir))
            for key in set(shallow) - {"API_APP"}:
                assert result[key] == shallow[key], (env, key)
