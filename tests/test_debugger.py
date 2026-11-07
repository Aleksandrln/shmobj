"""Плагин отладчика `pydevd_plugins/extensions/pydevd_plugin_shmobj.py` через pydevd PyCharm (`pydevd-pycharm`)."""

import pytest

import shmobj

pydevd_xml = pytest.importorskip("_pydevd_bundle.pydevd_xml")
from _pydevd_bundle.pydevd_utils import VariableWithOffset  # noqa: E402

DATA = {
    "vms": {i: {"cpu": i / 10, "name": f"vm{i}"} for i in range(100, 783)},
    "l": list(range(250)),
    b"raw": 1,
    2**70: "big",
}


@pytest.fixture
def root():
    return shmobj.loads(shmobj.dumps(DATA))


def children(v):
    _, _, resolver = pydevd_xml.get_type(v)
    return resolver, resolver.get_dictionary(v)


def test_dict_children_resolve_back(root):
    resolver, d = children(root)
    assert type(resolver).__name__ == "FrozenDictResolver"
    assert list(d) == ["'vms'", "'l'", "b'raw'", str(2**70), "__len__"]
    assert d["__len__"] == 4
    for name, v in d.items():
        if name != "__len__":
            assert resolver.resolve(root, name) == v


def test_dict_pages(root):
    vms = root["vms"]
    resolver, d = children(vms)
    assert list(d)[:2] == ["100", "101"] and len(d) == 101  # страница PyCharm — 100 детей
    assert resolver.resolve(vms, "100") == vms[100]
    assert list(resolver.get_dictionary(VariableWithOffset(vms, 600)))[:1] == ["700"]


@pytest.mark.parametrize("view", ["keys", "values", "items"])
def test_views_by_index(root, view):
    v = getattr(root["vms"], view)()
    resolver, d = children(v)
    assert list(d)[:2] == ["000", "001"] and d["__len__"] == 683
    assert resolver.resolve(v, "001") == list(v)[1]


def test_list(root):
    resolver, d = children(root["l"])
    assert list(d)[:2] == ["000", "001"] and d["__len__"] == 250
    assert resolver.resolve(root["l"], "249") == 249


def test_other_mapping_views_untouched():
    _, d = children({1: 2}.keys())
    assert "mapping" in d


def test_value_preview(root):
    xml = pydevd_xml.var_to_xml(root, "x")
    assert 'isContainer="True"' in xml and "%27vms%27%3A %3CFrozenDict len=683&gt;" in xml
    preview = pydevd_xml._str_from_providers(root["vms"].keys(), *pydevd_xml.get_type(root["vms"].keys())[:2])
    assert preview.startswith("KeysView([100, 101, ") and preview.endswith(", ...])")
