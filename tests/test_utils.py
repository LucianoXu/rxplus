import pytest

from rxplus.utils import TaggedData, get_short_error_info, get_full_error_info


def test_tagged_data_repr_str():
    td = TaggedData('tag', 123)
    assert str(td) == '(tag=tag, 123)'
    assert repr(td) == '(tag=tag, 123)'


def test_error_info_functions():
    try:
        raise ValueError('oops')
    except Exception as e:
        short = get_short_error_info(e)
        full = get_full_error_info(e)

    assert 'ValueError' in short and 'oops' in short
    assert 'ValueError' in full and 'oops' in full
