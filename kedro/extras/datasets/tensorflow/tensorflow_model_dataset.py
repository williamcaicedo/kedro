"""``TensorflowModelDataset`` is a data set implementation which can save and load
TensorFlow models.
"""
import copy
import tempfile
from pathlib import PurePath, PurePosixPath
from typing import Any, Dict

import fsspec
import tensorflow as tf

from kedro.io.core import (
    AbstractVersionedDataSet,
    DataSetError,
    Version,
    get_filepath_str,
    get_protocol_and_path,
)

TEMPORARY_H5_FILE = "tmp_tensorflow_model.h5"


class TensorFlowModelDataset(AbstractVersionedDataSet[tf.keras.Model, tf.keras.Model]):
    """``TensorflowModelDataset`` loads and saves TensorFlow models.
    The underlying functionality is supported by, and passes input arguments through to,
    TensorFlow 2.X load_model and save_model methods.

    Example:
    ::

        >>> from kedro.extras.datasets.tensorflow import TensorFlowModelDataset
        >>> import tensorflow as tf
        >>> import numpy as np
        >>>
        >>> data_set = TensorFlowModelDataset("saved_model_path")
        >>> model = tf.keras.Model()
        >>> predictions = model.predict([...])
        >>>
        >>> data_set.save(model)
        >>> loaded_model = data_set.load()
        >>> new_predictions = loaded_model.predict([...])
        >>> np.testing.assert_allclose(predictions, new_predictions, rtol=1e-6, atol=1e-6)

    """

    DEFAULT_LOAD_ARGS = {}  # type: Dict[str, Any]
    DEFAULT_SAVE_ARGS = {"save_format": "tf"}  # type: Dict[str, Any]

    # pylint: disable=too-many-arguments
    def __init__(
        self,
        filepath: str,
        load_args: Dict[str, Any] = None,
        save_args: Dict[str, Any] = None,
        version: Version = None,
        credentials: Dict[str, Any] = None,
        fs_args: Dict[str, Any] = None,
    ) -> None:
        """Creates a new instance of ``TensorFlowModelDataset``.

        Args:
            filepath: Filepath in POSIX format to a TensorFlow model directory prefixed with a
                protocol like `s3://`. If prefix is not provided `file` protocol (local filesystem)
                will be used. The prefix should be any protocol supported by ``fsspec``.
                Note: `http(s)` doesn't support versioning.
            load_args: TensorFlow options for loading models.
                Here you can find all available arguments:
                https://www.tensorflow.org/api_docs/python/tf/keras/models/load_model
                All defaults are preserved.
            save_args: TensorFlow options for saving models.
                Here you can find all available arguments:
                https://www.tensorflow.org/api_docs/python/tf/keras/models/save_model
                All defaults are preserved, except for "save_format", which is set to "tf".
            version: If specified, should be an instance of
                ``kedro.io.core.Version``. If its ``load`` attribute is
                None, the latest version will be loaded. If its ``save``
                attribute is None, save version will be autogenerated.
            credentials: Credentials required to get access to the underlying filesystem.
                E.g. for ``GCSFileSystem`` it should look like `{'token': None}`.
            fs_args: Extra arguments to pass into underlying filesystem class constructor
                (e.g. `{"project": "my-project"}` for ``GCSFileSystem``).
        """
        _fs_args = copy.deepcopy(fs_args) or {}
        _credentials = copy.deepcopy(credentials) or {}
        protocol, path = get_protocol_and_path(filepath, version)
        if protocol == "file":
            _fs_args.setdefault("auto_mkdir", True)

        self._protocol = protocol
        self._fs = fsspec.filesystem(self._protocol, **_credentials, **_fs_args)
        super().__init__(
            filepath=PurePosixPath(path),
            version=version,
            exists_function=self._fs.exists,
            glob_function=self._fs.glob,
        )

        self._tmp_prefix = "kedro_tensorflow_tmp"  # temp prefix pattern

        # Handle default load and save arguments
        self._load_args = copy.deepcopy(self.DEFAULT_LOAD_ARGS)
        if load_args is not None:
            self._load_args.update(load_args)
        self._save_args = copy.deepcopy(self.DEFAULT_SAVE_ARGS)
        if save_args is not None:
            self._save_args.update(save_args)

        self._is_h5 = self._save_args.get("save_format") == "h5"

    def _load(self) -> tf.keras.Model:
        load_path = get_filepath_str(self._get_load_path(), self._protocol)

        with tempfile.TemporaryDirectory(prefix=self._tmp_prefix) as path:
            if self._is_h5:
                path = str(PurePath(path) / TEMPORARY_H5_FILE)
                self._fs.copy(load_path, path)
            else:
                self._fs.get(load_path, path, recursive=True)

            # Pass the local temporary directory/file path to keras.load_model
            device = self._load_args.pop("tf_device", "gpu")
            if device == "cpu":
                with tf.device("/CPU:0"):
                    model = tf.keras.models.load_model(path, **self._load_args)
            else:
                model = tf.keras.models.load_model(path, **self._load_args)
            return model

    def _save(self, data: tf.keras.Model) -> None:
        save_path = get_filepath_str(self._get_save_path(), self._protocol)

        with tempfile.TemporaryDirectory(prefix=self._tmp_prefix) as path:
            if self._is_h5:
                path = str(PurePath(path) / TEMPORARY_H5_FILE)

            tf.keras.models.save_model(data, path, **self._save_args)

            # Use fsspec to take from local tempfile directory/file and
            # put in ArbitraryFileSystem
            if self._is_h5:
                self._fs.copy(path, save_path)
            else:
                if self._fs.exists(save_path):
                    self._fs.rm(save_path, recursive=True)
                self._fs.put(path, save_path + "//", recursive=True, overwrite=True)

    def _exists(self) -> bool:
        try:
            load_path = get_filepath_str(self._get_load_path(), self._protocol)
        except DataSetError:
            return False
        return self._fs.exists(load_path)

    def _describe(self) -> Dict[str, Any]:
        return dict(
            filepath=self._filepath,
            protocol=self._protocol,
            load_args=self._load_args,
            save_args=self._save_args,
            version=self._version,
        )

    def _release(self) -> None:
        super()._release()
        self._invalidate_cache()

    def _invalidate_cache(self) -> None:
        """Invalidate underlying filesystem caches."""
        filepath = get_filepath_str(self._filepath, self._protocol)
        self._fs.invalidate_cache(filepath)
