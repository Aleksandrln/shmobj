"""Эталонные входы. Rust-реализация обязана читать golden/*.bin и получать ровно эти значения."""
CASES = {
    "scalars": [None, True, False, 0, -1, 2**59 - 1, -2**59, 2**59, 2**63 - 1, -2**63, 2**64, -2**200,
                0.0, -0.0, 1.5, float("inf"), "", "abc", "1234567", "12345678", "юникод", "😀", b"", b"\x00\xff"],
    "typed_lists": {"u8": [0, 255], "i8": [-128, 127], "u16": [0, 65535], "i16": [-1, 300],
                    "u32": [0, 2**32 - 1], "i32": [-1, 2**31 - 1], "i64": [-2**63, 2**63 - 1],
                    "f64": [0.5, -2.0], "bool": [True, False, True], "mixed": [1, True, 1.0, "1"]},
    "keys": {None: 0, True: 1, 2: 2, 2.5: 3, "s": 4, b"b": 5, -2**70: 6, "длинный ключ": 7},
    "sorted_dict": {f"k{i}": i for i in range(100)},
    "records": [{"id": i, "name": f"user_{i}", "score": i / 3, "tags": ["a", "bb"]} for i in range(50)],
    "nested": {"a": [[], {}, [[]], {"b": {"c": [1, 2, {"d": None}]}}]},
    "empty_dict": {},
    "empty_list": [],
    "root_scalar": "just a string at root",
}
