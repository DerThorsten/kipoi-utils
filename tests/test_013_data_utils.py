"""Test data_utils
"""

import pytest
from pytest import fixture
from kipoi_utils.data_utils import get_dataset_lens, get_dataset_item
import numpy as np


@fixture
def data():
    return {"a": [np.arange(3)],
            "b": {"d": np.arange(3)},
            "c": np.arange(3).reshape((-1, 1))
            }


@fixture
def bad_data():
    return {"a": [np.arange(3)],
            "b": {"d": np.arange(4)},
            "c": np.arange(3).reshape((-1, 1)),
            "e": 1
            }


# data = data()
# bad_data = bad_data()


def test_datset_lens_good(data):
    assert get_dataset_lens(data) == 3 * [3]


def test_datset_lens_bad(bad_data):
    assert sorted(get_dataset_lens(bad_data)) == [1, 3, 3, 4]

    with pytest.raises(Exception):
        get_dataset_lens(bad_data, require_numpy=True)


def test_get_item(data):
    dlen = get_dataset_lens(data)[0]
    assert dlen == 3
    assert len(set(get_dataset_lens(data))) == 1
    assert get_dataset_item(data, 1) == {"a": [1], "b": {"d": 1}, "c": np.array([1])}


@pytest.mark.skip(reason="is a kipoi test, not kipoi_utils test")
def test_preloaded_dataset(data):
    def data_fn():
        return data

    d = PreloadedDataset.from_fn(data_fn)()

    assert d.load_all() == data
    assert len(d) == 3
    assert d[1] == {"a": [1], "b": {"d": 1}, "c": np.array([1])}
    assert list(d.batch_iter(2))[1] == {'a': [np.array([2])], 'b': {'d': np.array([2])}, 'c': np.array([[2]])}
