# Copyright 2021 QuantumBlack Visual Analytics Limited
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES
# OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND
# NONINFRINGEMENT. IN NO EVENT WILL THE LICENSOR OR OTHER CONTRIBUTORS
# BE LIABLE FOR ANY CLAIM, DAMAGES, OR OTHER LIABILITY, WHETHER IN AN
# ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF, OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#
# The QuantumBlack Visual Analytics Limited ("QuantumBlack") name and logo
# (either separately or in combination, "QuantumBlack Trademarks") are
# trademarks of QuantumBlack. The License does not grant you any right or
# license to the QuantumBlack Trademarks. You may not use the QuantumBlack
# Trademarks or any confusingly similar mark as a trademark for your product,
# or use the QuantumBlack Trademarks in any other manner that might cause
# confusion in the marketplace, including but not limited to in advertising,
# on websites, or on software.
#
# See the License for the specific language governing permissions and
# limitations under the License.
"""This module provides ``kedro.config`` with the functionality to load one
or more configuration files from specified paths.
"""
import logging
from glob import iglob
from pathlib import Path
from typing import AbstractSet, Any, Dict, Iterable, List, Set
from warnings import warn

SUPPORTED_EXTENSIONS = [
    ".yml",
    ".yaml",
    ".json",
    ".ini",
    ".pickle",
    ".properties",
    ".xml",
]


class MissingConfigException(Exception):
    """Raised when no configuration files can be found within a config path"""

    pass


class ConfigLoader:
    """Recursively scan directories (config paths) contained in ``conf_root`` for
    configuration files with a ``yaml``, ``yml``, ``json``, ``ini``,
    ``pickle``, ``xml`` or ``properties`` extension, load them,
    and return them in the form of a config dictionary.

    The first processed config path is the ``base`` directory inside
    ``conf_root``. The optional ``env`` argument can be used to specify a
    subdirectory of ``conf_root`` to process as a config path after ``base``.

    When the same top-level key appears in any 2 config files located in
    the same (sub)directory, a ``ValueError`` is raised.

    When the same key appears in any 2 config files located in different
    (sub)directories, the last processed config path takes precedence
    and overrides this key.

    For example, if your ``conf_root`` looks like this:
    ::

        .
        `-- conf
            |-- README.md
            |-- base
            |   |-- catalog.yml
            |   |-- logging.yml
            |   `-- experiment1
            |       `-- parameters.yml
            `-- local
                |-- catalog.yml
                |-- db.ini
                |-- experiment1
                |   |-- parameters.yml
                |   `-- model_parameters.yml
                `-- experiment2
                    `-- parameters.yml


    You can access the different configurations as follows:
    ::

        >>> import logging.config
        >>> from kedro.config import ConfigLoader
        >>>
        >>> conf_loader = ConfigLoader('conf', 'local')
        >>>
        >>> conf_logging = conf_loader.get('logging*')
        >>> logging.config.dictConfig(conf_logging)  # set logging conf
        >>>
        >>> conf_catalog = conf_loader.get('catalog*', 'catalog*/**')
        >>> conf_params = conf_loader.get('**/parameters.yml')

    """

    def __init__(
        self, conf_root: str, env: str = None, extra_params: Dict[str, Any] = None
    ):
        """Instantiates a ``ConfigLoader``.

        Args:
            conf_root: Path to use as root directory for loading configuration.
            env: Environment that will take precedence over base.
            extra_params: Extra parameters passed to a Kedro run.
        """
        self.conf_paths = _remove_duplicates(
            self._build_conf_paths(Path(conf_root), env)
        )
        self.logger = logging.getLogger(__name__)
        self.extra_params = extra_params

    @staticmethod
    def _build_conf_paths(conf_root: Path, env: str = None) -> List[str]:
        """Builds list of paths to use for configuration."""
        if not env:
            return [str(conf_root / "base")]
        return [str(conf_root / "base"), str(conf_root / env)]

    @staticmethod
    def _load_config_file(config_file: Path) -> Dict[str, Any]:
        """Load an individual config file using `anyconfig` as a backend.

        Args:
            config_file: Path to a config file to process.

        Returns:
            Parsed configuration.
        """
        # for performance reasons
        import anyconfig  # pylint: disable=import-outside-toplevel

        # Default to UTF-8, which is Python 3 default encoding, to decode the file
        with open(config_file, encoding="utf8") as yml:
            return {
                k: v for k, v in anyconfig.load(yml).items() if not k.startswith("_")
            }

    def _load_configs(self, config_filepaths: List[Path]) -> Dict[str, Any]:
        """Recursively load all configuration files, which satisfy
        a given list of glob patterns from a specific path.

        Args:
            config_filepaths: Configuration files sorted in the order of precedence.

        Raises:
            ValueError: If 2 or more configuration files contain the same key(s).

        Returns:
            Resulting configuration dictionary.

        """

        aggregate_config = {}
        seen_file_to_keys = {}  # type: Dict[Path, AbstractSet[str]]

        for config_filepath in config_filepaths:
            single_config = self._load_config_file(config_filepath)
            _check_duplicate_keys(seen_file_to_keys, config_filepath, single_config)
            seen_file_to_keys[config_filepath] = single_config.keys()
            aggregate_config.update(single_config)

        return aggregate_config

    def _lookup_config_filepaths(
        self, conf_path: Path, patterns: Iterable[str], processed_files: Set[Path]
    ) -> List[Path]:
        config_files = _path_lookup(conf_path, patterns)

        seen_files = config_files & processed_files
        if seen_files:
            self.logger.warning(
                "Config file(s): %s already processed, skipping loading...",
                ", ".join(str(seen) for seen in sorted(seen_files)),
            )
            config_files -= seen_files

        return sorted(config_files)

    def get(self, *patterns: str) -> Dict[str, Any]:
        """Recursively scan for configuration files, load and merge them, and
        return them in the form of a config dictionary.

        Args:
            patterns: Glob patterns to match. Files, which names match
                any of the specified patterns, will be processed.

        Raises:
            ValueError: If 2 or more configuration files inside the same
                config path (or its subdirectories) contain the same
                top-level key.
            MissingConfigException: If no configuration files exist within
                a specified config path.

        Returns:
            Dict[str, Any]:  A Python dictionary with the combined
                configuration from all configuration files. **Note:** any keys
                that start with `_` will be ignored.
        """

        if not patterns:
            raise ValueError(
                "`patterns` must contain at least one glob "
                "pattern to match config filenames against."
            )

        config = {}  # type: Dict[str, Any]
        processed_files = set()  # type: Set[Path]

        for conf_path in self.conf_paths:
            if not Path(conf_path).is_dir():
                raise ValueError(
                    f"Given configuration path either does not exist "
                    f"or is not a valid directory: {conf_path}"
                )

            config_filepaths = self._lookup_config_filepaths(
                Path(conf_path), patterns, processed_files
            )
            new_conf = self._load_configs(config_filepaths)

            common_keys = config.keys() & new_conf.keys()
            if common_keys:
                sorted_keys = ", ".join(sorted(common_keys))
                msg = (
                    "Config from path `%s` will override the following "
                    "existing top-level config keys: %s"
                )
                self.logger.info(msg, conf_path, sorted_keys)

            config.update(new_conf)
            processed_files |= set(config_filepaths)

        if not processed_files:
            raise MissingConfigException(
                f"No files found in {self.conf_paths} matching the glob "
                f"pattern(s): {list(patterns)}"
            )
        return config


def _check_duplicate_keys(
    processed_files: Dict[Path, AbstractSet[str]], filepath: Path, conf: Dict[str, Any]
) -> None:
    duplicates = []

    for processed_file, keys in processed_files.items():
        overlapping_keys = conf.keys() & keys

        if overlapping_keys:
            sorted_keys = ", ".join(sorted(overlapping_keys))
            if len(sorted_keys) > 100:
                sorted_keys = sorted_keys[:100] + "..."
            duplicates.append(f"{processed_file}: {sorted_keys}")

    if duplicates:
        dup_str = "\n- ".join(duplicates)
        raise ValueError(f"Duplicate keys found in {filepath} and:\n- {dup_str}")


def _path_lookup(conf_path: Path, patterns: Iterable[str]) -> Set[Path]:
    """Return a set of all configuration files from ``conf_path`` or
    its subdirectories, which satisfy a given list of glob patterns.

    Args:
        conf_path: Path to configuration directory.
        patterns: List of glob patterns to match the filenames against.

    Returns:
        A set of paths to configuration files.

    """
    config_files = set()
    conf_path = conf_path.resolve()

    for pattern in patterns:
        # `Path.glob()` ignores the files if pattern ends with "**",
        # therefore iglob is used instead
        for each in iglob(str(conf_path / pattern), recursive=True):
            path = Path(each).resolve()
            if path.is_file() and path.suffix in SUPPORTED_EXTENSIONS:
                config_files.add(path)

    return config_files


def _remove_duplicates(items: Iterable[str]):
    """Remove duplicates while preserving the order."""
    unique_items = []  # type: List[str]
    for item in items:
        if item not in unique_items:
            unique_items.append(item)
        else:
            warn(
                f"Duplicate environment detected! "
                f"Skipping re-loading from configuration path: {item}"
            )
    return unique_items
