//! Узлы скаляров (doc/spec/03-format.md §3). Тип известен из тега слота.
//!
//! | Узел   | Раскладка |
//! |--------|-----------|
//! | INT64  | `i64` |
//! | FLOAT  | `f64` |
//! | BIGINT | `u32 nbytes`, `u32 0`, байты (дополнение до двух, LE) |
//! | STR    | `u32 byte_len`, `u32 char_count`, UTF-8 |
//! | BYTES  | `u64 len`, байты |

/// Сериализация узла в `out` (заполнение до 8 байт делает вызывающий).
pub fn encode_int64(out: &mut Vec<u8>, v: i64) {
    out.extend_from_slice(&v.to_le_bytes());
}

pub fn encode_float(out: &mut Vec<u8>, v: f64) {
    out.extend_from_slice(&v.to_le_bytes());
}

pub fn encode_bigint(out: &mut Vec<u8>, le: &[u8]) {
    out.extend_from_slice(&(le.len() as u32).to_le_bytes());
    out.extend_from_slice(&0u32.to_le_bytes());
    out.extend_from_slice(le);
}

pub fn encode_str(out: &mut Vec<u8>, utf8: &[u8], char_count: u32) {
    out.extend_from_slice(&(utf8.len() as u32).to_le_bytes());
    out.extend_from_slice(&char_count.to_le_bytes());
    out.extend_from_slice(utf8);
}

pub fn encode_bytes(out: &mut Vec<u8>, b: &[u8]) {
    out.extend_from_slice(&(b.len() as u64).to_le_bytes());
    out.extend_from_slice(b);
}

/// Целое как в `int.to_bytes((v.bit_length() + 8) // 8, "little", signed=True)`.
/// Возвращает буфер и длину (до 17 байт для i128).
pub fn i128_le_bytes(v: i128) -> ([u8; 17], usize) {
    let bits = 128 - v.unsigned_abs().leading_zeros() as usize;
    let n = (bits + 8) / 8;
    let mut buf = [if v < 0 { 0xFF } else { 0 }; 17];
    if let Some(dst) = buf.get_mut(..16) {
        dst.copy_from_slice(&v.to_le_bytes());
    }
    (buf, n)
}

/// Обратное к [`i128_le_bytes`] для чисел, помещающихся в i128.
pub fn le_bytes_to_i128(b: &[u8]) -> Option<i128> {
    if b.is_empty() {
        return Some(0);
    }
    let neg = b.last().is_some_and(|&x| x & 0x80 != 0);
    let fill = if neg { 0xFF } else { 0 };
    if b.len() > 16 {
        // Лишние старшие байты допустимы, только если это расширение знака.
        let (lo, hi) = b.split_at(16);
        if hi.iter().any(|&x| x != fill) || (lo.last().is_some_and(|&x| x & 0x80 != 0) != neg) {
            return None;
        }
        return le_bytes_to_i128(lo);
    }
    let mut buf = [fill; 16];
    if let Some(dst) = buf.get_mut(..b.len()) {
        dst.copy_from_slice(b);
    }
    Some(i128::from_le_bytes(buf))
}

/// Байты целого значения конечного `f`, где `f.fract() == 0` и |f| ≥ 2^127,
/// по тому же правилу длины, что [`i128_le_bytes`].
pub fn big_float_int_bytes(f: f64) -> Vec<u8> {
    let bits = f.to_bits();
    let exp = ((bits >> 52) & 0x7FF) as i64 - 1075;
    let mant = (bits & ((1u64 << 52) - 1)) | (1u64 << 52);
    // |f| = mant << exp, exp ≥ 0 для таких больших чисел.
    let shift = exp.max(0) as usize;
    let bitlen = 53 + shift;
    let n = (bitlen + 8) / 8;
    let mut mag = vec![0u8; n];
    let (byte_shift, bit_shift) = (shift / 8, shift % 8);
    let wide = u128::from(mant) << bit_shift;
    for (i, b) in wide.to_le_bytes().iter().enumerate() {
        if let Some(dst) = mag.get_mut(byte_shift + i) {
            *dst = *b;
        }
    }
    if f.is_sign_negative() {
        let mut carry = 1u16;
        for b in mag.iter_mut() {
            let v = u16::from(!*b) + carry;
            *b = v as u8;
            carry = v >> 8;
        }
    }
    mag
}
