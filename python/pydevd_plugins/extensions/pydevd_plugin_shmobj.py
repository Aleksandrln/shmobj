"""Содержимое `FrozenDict`, `FrozenList` и представлений `FrozenDict` в отладчике (pydevd: PyCharm, VS Code).

pydevd сам загружает модули `pydevd_plugins/extensions/pydevd_plugin*` из `sys.path`. Без плагина
отладчик разворачивает прокси в атрибуты, а данных в атрибутах нет.
Методы pydevd не показывает ни у каких объектов — у прокси они в узле `methods`: сигнатура и первая
строка докстринга.
Ключ ребёнка в словаре — `repr` ключа, `resolve` разбирает его обратно (`ast.literal_eval`): ключи
`FrozenDict` при каждом чтении — новые объекты, поэтому `id(key)`, как у `dict`, не подходит.
"""
import ast
from collections.abc import ItemsView, KeysView, ValuesView
from functools import cache
from itertools import islice

from _pydevd_bundle.pydevd_extension_api import StrPresentationProvider, TypeResolveProvider
from _pydevd_bundle.pydevd_resolver import defaultResolver

try:  # PyCharm: детей отдаёт страницами
    from _pydevd_bundle.pydevd_resolver import MAX_ITEMS_TO_HANDLE
    from _pydevd_bundle.pydevd_utils import get_var_and_offset
except ImportError:  # debugpy (VS Code)
    MAX_ITEMS_TO_HANDLE = 300

    def get_var_and_offset(var):
        return var, 0


_PREVIEW_CHARS = 300
_LEN = '__len__'
_METHODS = 'methods'


def _is_shmobj(type_object, name):
    return type_object.__name__ == name and type_object.__module__ == 'shmobj'


def _is_frozen_view(var):
    return _is_shmobj(type(getattr(var, '_mapping', None)), 'FrozenDict')


def _page(var, items):
    var, offset = get_var_and_offset(var)
    return var, islice(items(var), offset, offset + MAX_ITEMS_TO_HANDLE), offset


class _Methods:
    """Узел `methods`: pydevd не показывает вызываемые атрибуты ни у каких объектов."""

    def __init__(self, cls):
        self.cls = cls


@cache
def _signatures(cls):
    out = {}
    for name in dir(cls):
        if not name.startswith('_'):
            f = getattr(cls, name)
            sig = (getattr(f, '__text_signature__', None) or '()').replace('$self, ', '').replace('$self', '')
            doc = (f.__doc__ or '').partition('\n')[0]
            out[name] = f'{sig} — {doc}' if doc else sig
    return out


def _with_extras(var, out):
    if type(var).__module__ == 'shmobj':  # у представлений методы стандартные
        out[_METHODS] = _Methods(type(var))
    out[_LEN] = len(var)
    return out


def _resolve_extra(var, attribute):
    return _Methods(type(var)) if attribute == _METHODS else len(var)


def _by_index(var, items):
    var, page, offset = _page(var, items)
    width = len(str(len(var)))
    return _with_extras(var, {f'{i:0{width}d}': v for i, v in enumerate(page, offset)})


def _preview(parts, do_trim):
    out, size = [], 0
    for part in parts:
        if do_trim and size > _PREVIEW_CHARS:
            out.append('...')
            break
        out.append(part)
        size += len(part) + 2
    return ', '.join(out)


class FrozenDictResolver(TypeResolveProvider):
    def can_provide(self, type_object, type_name):
        return _is_shmobj(type_object, 'FrozenDict')

    def resolve(self, var, attribute):
        if attribute in (_LEN, _METHODS):
            return _resolve_extra(var, attribute)
        return var[ast.literal_eval(attribute)]

    def get_dictionary(self, var):
        var, page, _ = _page(var, lambda d: d.iter_items())
        return _with_extras(var, {repr(k): v for k, v in page})


class FrozenListResolver(TypeResolveProvider):
    def can_provide(self, type_object, type_name):
        return _is_shmobj(type_object, 'FrozenList')

    def resolve(self, var, attribute):
        return _resolve_extra(var, attribute) if attribute in (_LEN, _METHODS) else var[int(attribute)]

    def get_dictionary(self, var):
        return _by_index(var, iter)


class FrozenDictViewResolver(TypeResolveProvider):
    """`keys()`/`values()`/`items()` — списком; представления других `Mapping` — как без плагина."""

    def can_provide(self, type_object, type_name):
        return type_object in (KeysView, ValuesView, ItemsView)

    def resolve(self, var, attribute):
        if not _is_frozen_view(var):
            return defaultResolver.resolve(var, attribute)
        if attribute == _LEN:
            return len(var)
        return next(islice(var, int(attribute), None))

    def get_dictionary(self, var):
        data = get_var_and_offset(var)[0]
        if not _is_frozen_view(data):
            return defaultResolver.get_dictionary(data)
        return _by_index(var, iter)


class MethodsResolver(TypeResolveProvider):
    def can_provide(self, type_object, type_name):
        return type_object is _Methods

    def resolve(self, var, attribute):
        return _signatures(var.cls)[attribute]

    def get_dictionary(self, var):
        return dict(_signatures(var.cls))


class FrozenStr(StrPresentationProvider):
    """Начало содержимого вместо `<FrozenDict len=N>`; вложенные прокси — своим `repr`."""

    def can_provide(self, type_object, type_name):
        return (
            _is_shmobj(type_object, 'FrozenDict')
            or _is_shmobj(type_object, 'FrozenList')
            or type_object in (KeysView, ValuesView, ItemsView, _Methods)
        )

    def get_str(self, val, do_trim=True):
        try:
            return self._content(val, do_trim)
        except Exception:  # закрытый сегмент
            return repr(val)

    def _content(self, val, do_trim):
        if isinstance(val, _Methods):
            return ', '.join(_signatures(val.cls))
        if isinstance(val, (KeysView, ValuesView, ItemsView)):
            if not _is_frozen_view(val):
                return repr(val)
            return f'{type(val).__name__}([{_preview(map(repr, val), do_trim)}])'
        if _is_shmobj(type(val), 'FrozenList'):
            return f'[{_preview(map(repr, val), do_trim)}]'
        return '{' + _preview((f'{k!r}: {v!r}' for k, v in val.iter_items()), do_trim) + '}'
