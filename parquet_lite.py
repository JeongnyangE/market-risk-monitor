"""
parquet_lite — 의존성 없는 최소 Parquet 리더 (pyarrow/fastparquet 미설치 환경용 폴백)

지원 범위 (pyarrow 기본 출력 기준):
  - 평면(flat) 스키마, OPTIONAL/REQUIRED 컬럼
  - 물리 타입: BOOLEAN, INT32, INT64, FLOAT, DOUBLE, BYTE_ARRAY
  - 인코딩: PLAIN, PLAIN_DICTIONARY / RLE_DICTIONARY, RLE(정의레벨)
  - 데이터 페이지 V1 / V2
  - 압축: UNCOMPRESSED, SNAPPY(C 헬퍼), GZIP(zlib)

pyarrow가 설치돼 있으면 data.read_parquet()가 pandas.read_parquet를 우선 사용하므로
이 모듈은 폴백 경로로만 쓰인다.
"""
from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import tempfile
import zlib

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# C 헬퍼: snappy 해제 + RLE/bit-packed 하이브리드 디코딩
# ---------------------------------------------------------------------------
_C_SRC = r"""
#include <stdint.h>
#include <string.h>
#include <stddef.h>

static int read_varint(const uint8_t* p, size_t n, size_t* pos, uint64_t* out){
    uint64_t r=0; int shift=0;
    while(*pos<n){ uint8_t b=p[(*pos)++]; r|=((uint64_t)(b&0x7f))<<shift;
        if(!(b&0x80)){*out=r; return 0;} shift+=7; if(shift>63) return -1; }
    return -1;
}

/* returns uncompressed length written, or -1 on error */
int64_t snappy_decompress(const uint8_t* src, size_t n, uint8_t* dst, size_t cap){
    size_t ip=0, op=0; uint64_t ulen;
    if(read_varint(src,n,&ip,&ulen)) return -1;
    if(ulen>cap) return -2;
    while(ip<n){
        uint8_t tag=src[ip++]; uint32_t t=tag&3;
        if(t==0){
            size_t len=(tag>>2);
            if(len>=60){ int nb=(int)len-59; len=0; for(int i=0;i<nb;i++) len|=((size_t)src[ip+i])<<(8*i); ip+=nb; }
            len+=1;
            if(ip+len>n || op+len>ulen) return -3;
            memcpy(dst+op,src+ip,len); ip+=len; op+=len;
        } else {
            size_t len, off;
            if(t==1){ len=4+((tag>>2)&7); off=((size_t)(tag>>5)<<8)|src[ip]; ip+=1; }
            else if(t==2){ len=(tag>>2)+1; off=src[ip]|((size_t)src[ip+1]<<8); ip+=2; }
            else { len=(tag>>2)+1; off=src[ip]|((size_t)src[ip+1]<<8)|((size_t)src[ip+2]<<16)|((size_t)src[ip+3]<<24); ip+=4; }
            if(off==0 || off>op || op+len>ulen) return -4;
            uint8_t* d=dst+op; const uint8_t* s=d-off;
            if(off>=len) memcpy(d,s,len); else for(size_t i=0;i<len;i++) d[i]=s[i];
            op+=len;
        }
    }
    return (int64_t)op;
}

/* RLE/bit-packed hybrid; decodes up to `count` values into out; returns bytes consumed or -1 */
int64_t rle_hybrid_decode(const uint8_t* src, size_t n, int bw, int32_t* out, int64_t count){
    size_t ip=0; int64_t k=0; int bytew=(bw+7)/8;
    while(k<count && ip<n){
        uint64_t h; if(read_varint(src,n,&ip,&h)) return -1;
        if(h&1){
            int64_t groups=(int64_t)(h>>1); int64_t nv=groups*8;
            size_t nbytes=(size_t)groups*bw;
            if(ip+nbytes>n) nbytes=n-ip;
            uint64_t buf=0; int bits=0; size_t bp=ip; uint32_t mask = bw==32?0xffffffffu:((1u<<bw)-1u);
            for(int64_t i=0;i<nv;i++){
                while(bits<bw){ buf|= (bp<ip+nbytes? (uint64_t)src[bp]:0)<<bits; bp++; bits+=8; }
                if(k<count){ out[k++]=(int32_t)(buf&mask); }
                if(bw==0){ continue; }
                buf>>=bw; bits-=bw;
            }
            ip+= (size_t)groups*bw;
        } else {
            int64_t run=(int64_t)(h>>1); uint32_t v=0;
            for(int i=0;i<bytew;i++) v|=((uint32_t)src[ip+i])<<(8*i);
            ip+=bytew;
            for(int64_t i=0;i<run && k<count;i++) out[k++]=(int32_t)v;
        }
    }
    return (int64_t)ip;
}

/* PLAIN BYTE_ARRAY: compute offsets for `count` values; returns bytes consumed */
int64_t plain_bytearray_offsets(const uint8_t* src, size_t n, int64_t count, int64_t* starts, int32_t* lens){
    size_t ip=0;
    for(int64_t i=0;i<count;i++){
        if(ip+4>n) return -1;
        uint32_t L=src[ip]|((uint32_t)src[ip+1]<<8)|((uint32_t)src[ip+2]<<16)|((uint32_t)src[ip+3]<<24);
        ip+=4; starts[i]=(int64_t)ip; lens[i]=(int32_t)L; ip+=L;
    }
    return (int64_t)ip;
}
"""

_LIB = None


def _lib():
    global _LIB
    if _LIB is not None:
        return _LIB
    cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "mrm")
    os.makedirs(cache_dir, exist_ok=True)
    so = os.path.join(cache_dir, "parquet_lite_helpers.so")
    if not os.path.exists(so):
        with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
            f.write(_C_SRC)
            c_path = f.name
        cc = os.environ.get("CC", "gcc")
        subprocess.check_call([cc, "-O3", "-shared", "-fPIC", "-o", so, c_path])
        os.unlink(c_path)
    lib = ctypes.CDLL(so)
    lib.snappy_decompress.restype = ctypes.c_int64
    lib.snappy_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
    lib.rle_hybrid_decode.restype = ctypes.c_int64
    lib.rle_hybrid_decode.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_void_p, ctypes.c_int64]
    lib.plain_bytearray_offsets.restype = ctypes.c_int64
    lib.plain_bytearray_offsets.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int64, ctypes.c_void_p, ctypes.c_void_p]
    _LIB = lib
    return lib


def _ptr(b):
    if isinstance(b, np.ndarray):
        return b.ctypes.data
    return ctypes.cast(ctypes.c_char_p(b), ctypes.c_void_p).value


# ---------------------------------------------------------------------------
# Thrift compact protocol
# ---------------------------------------------------------------------------
class _TReader:
    __slots__ = ("b", "p")

    def __init__(self, b: bytes, p: int = 0):
        self.b = b
        self.p = p

    def varint(self) -> int:
        r = 0
        s = 0
        b = self.b
        while True:
            x = b[self.p]
            self.p += 1
            r |= (x & 0x7F) << s
            if not x & 0x80:
                return r
            s += 7

    def zz(self) -> int:
        v = self.varint()
        return (v >> 1) ^ -(v & 1)

    def value(self, t: int):
        if t == 1:
            return True
        if t == 2:
            return False
        if t == 3:
            v = self.b[self.p]
            self.p += 1
            return v - 256 if v > 127 else v
        if t in (4, 5, 6):
            return self.zz()
        if t == 7:
            v = struct.unpack_from("<d", self.b, self.p)[0]
            self.p += 8
            return v
        if t == 8:
            n = self.varint()
            v = self.b[self.p:self.p + n]
            self.p += n
            return v
        if t in (9, 10):
            h = self.b[self.p]
            self.p += 1
            size = h >> 4
            et = h & 0x0F
            if size == 15:
                size = self.varint()
            out = []
            for _ in range(size):
                if et in (1, 2):  # bool in list: one byte
                    v = self.b[self.p]
                    self.p += 1
                    out.append(v == 1)
                else:
                    out.append(self.value(et))
            return out
        if t == 11:
            size = self.varint()
            out = {}
            if size:
                kv = self.b[self.p]
                self.p += 1
                kt, vt = kv >> 4, kv & 0x0F
                for _ in range(size):
                    k = self.value(kt)
                    out[k] = self.value(vt)
            return out
        if t == 12:
            return self.struct()
        raise ValueError(f"unknown thrift type {t}")

    def struct(self) -> dict:
        out = {}
        last = 0
        while True:
            h = self.b[self.p]
            self.p += 1
            if h == 0:
                return out
            t = h & 0x0F
            d = h >> 4
            fid = last + d if d else self.zz()
            last = fid
            out[fid] = self.value(t)


# ---------------------------------------------------------------------------
_DTYPES = {1: "<i4", 2: "<i8", 4: "<f4", 5: "<f8"}


def _decompress(codec: int, data: bytes, usize: int) -> bytes:
    if codec == 0:
        return data
    if codec == 1:
        out = np.empty(usize, dtype=np.uint8)
        n = _lib().snappy_decompress(_ptr(data), len(data), out.ctypes.data, usize)
        if n < 0:
            raise ValueError(f"snappy error {n}")
        return out[:n].tobytes()
    if codec == 2:
        return zlib.decompress(data, 16 + zlib.MAX_WBITS)
    raise NotImplementedError(f"codec {codec} not supported (install pyarrow)")


def _rle(buf: bytes, bw: int, count: int) -> tuple[np.ndarray, int]:
    out = np.empty(count, dtype=np.int32)
    if count == 0:
        return out, 0
    used = _lib().rle_hybrid_decode(_ptr(buf), len(buf), bw, out.ctypes.data, count)
    if used < 0:
        raise ValueError("rle decode error")
    return out, used


def _plain(ptype: int, buf: bytes, count: int):
    if ptype in _DTYPES:
        dt = np.dtype(_DTYPES[ptype])
        return np.frombuffer(buf, dtype=dt, count=count)
    if ptype == 0:
        bits = np.unpackbits(np.frombuffer(buf, dtype=np.uint8), bitorder="little")
        return bits[:count].astype(bool)
    if ptype == 6:
        starts = np.empty(count, dtype=np.int64)
        lens = np.empty(count, dtype=np.int32)
        if count:
            used = _lib().plain_bytearray_offsets(_ptr(buf), len(buf), count, starts.ctypes.data, lens.ctypes.data)
            if used < 0:
                raise ValueError("byte array decode error")
        return np.array([buf[s:s + l].decode("utf-8", "replace") for s, l in zip(starts, lens)], dtype=object)
    raise NotImplementedError(f"physical type {ptype}")


def read_metadata(path: str) -> dict:
    with open(path, "rb") as f:
        f.seek(-8, 2)
        tail = f.read(8)
        mlen = struct.unpack("<I", tail[:4])[0]
        if tail[4:] != b"PAR1":
            raise ValueError("not a parquet file")
        f.seek(-8 - mlen, 2)
        return _TReader(f.read(mlen)).struct()


def _schema(meta: dict) -> dict:
    cols = {}
    for el in meta[2][1:]:
        name = el[4].decode()
        cols[name] = {
            "ptype": el.get(1),
            "optional": el.get(3, 0) == 1,
            "converted": el.get(6),
            "logical": el.get(10),
        }
    return cols


def _read_chunk(f, cm: dict, info: dict, num_rows: int):
    ptype = cm[1]
    codec = cm[4]
    start = cm.get(11) if cm.get(11) is not None else cm[9]
    start = min(start, cm[9])
    f.seek(start)
    raw = f.read(cm[7])
    r = _TReader(raw, 0)
    dictionary = None
    parts_vals = []
    parts_def = []
    got = 0
    total = cm[5]
    while got < total and r.p < len(raw):
        ph = r.struct()
        ptype_page = ph[1]
        usize, csize = ph[2], ph[3]
        body = raw[r.p:r.p + csize]
        r.p += csize
        if ptype_page == 2:  # dictionary
            dh = ph[7]
            data = _decompress(codec, body, usize)
            dictionary = _plain(ptype, data, dh[1])
            continue
        if ptype_page == 0:  # data page v1
            dh = ph[5]
            nv, enc = dh[1], dh[2]
            data = _decompress(codec, body, usize)
            pos = 0
            if info["optional"]:
                ln = struct.unpack_from("<I", data, 0)[0]
                defs, _ = _rle(data[4:4 + ln], 1, nv)
                pos = 4 + ln
            else:
                defs = np.ones(nv, dtype=np.int32)
            vals_buf = data[pos:]
        elif ptype_page == 3:  # data page v2
            dh = ph[8]
            nv, nnull, enc = dh[1], dh[2], dh[4]
            dl, rl = dh.get(5, 0), dh.get(6, 0)
            levels = body[:rl + dl]
            payload = body[rl + dl:]
            compressed = dh.get(7, True)
            payload = _decompress(codec, payload, usize - rl - dl) if compressed else payload
            if info["optional"] and dl:
                defs, _ = _rle(levels[rl:rl + dl], 1, nv)
            else:
                defs = np.ones(nv, dtype=np.int32)
            vals_buf = payload
        else:
            continue
        nn = int(defs.sum())
        if enc in (2, 8):
            bw = vals_buf[0]
            idx, _ = _rle(vals_buf[1:], bw, nn)
            vals = ("dict", idx)
        elif enc == 0:
            vals = ("plain", _plain(ptype, vals_buf, nn))
        else:
            raise NotImplementedError(f"encoding {enc}")
        parts_vals.append(vals)
        parts_def.append(defs)
        got += nv
    return dictionary, parts_vals, parts_def


def read_parquet(path: str, columns: list[str] | None = None) -> pd.DataFrame:
    meta = read_metadata(path)
    schema = _schema(meta)
    names = list(schema.keys())
    want = columns or names
    out = {c: [] for c in want}
    with open(path, "rb") as f:
        for rg in meta[4]:
            nrows = rg[3]
            for cc in rg[1]:
                cm = cc[3]
                cname = b".".join(cm[3]).decode()
                if cname not in out:
                    continue
                info = schema[cname]
                dictionary, parts_vals, parts_def = _read_chunk(f, cm, info, nrows)
                out[cname].append((info, dictionary, parts_vals, parts_def))
    frame = {}
    for c in want:
        info = schema[c]
        is_str = info["ptype"] == 6
        segs = []
        for (_, dictionary, parts_vals, parts_def) in out[c]:
            for (kind, v), defs in zip(parts_vals, parts_def):
                mask = defs.astype(bool)
                if kind == "dict":
                    if is_str:
                        codes = np.full(len(defs), -1, dtype=np.int32)
                        codes[mask] = v
                        cats = pd.Index(dictionary)
                        if cats.is_unique:
                            segs.append(pd.Categorical.from_codes(codes, categories=cats))
                        else:
                            arr = np.empty(len(defs), dtype=object)
                            arr[:] = None
                            arr[mask] = dictionary[v]
                            segs.append(pd.Categorical(arr))
                        continue
                    vals = dictionary[v]
                else:
                    vals = v
                if is_str:
                    arr = np.empty(len(defs), dtype=object)
                    arr[:] = None
                    arr[mask] = vals
                    segs.append(pd.Categorical(arr))
                else:
                    if mask.all():
                        segs.append(np.asarray(vals))
                    else:
                        arr = np.full(len(defs), np.nan)
                        arr[mask] = vals
                        segs.append(arr)
        if is_str:
            col = pd.api.types.union_categoricals(segs, ignore_order=True) if len(segs) > 1 else segs[0]
            frame[c] = col
        else:
            col = np.concatenate(segs) if segs else np.array([])
            lg = info["logical"]
            if lg and 8 in lg:  # TIMESTAMP logical type
                unit_field = lg[8].get(2, {})
                unit = "ms" if 1 in unit_field else ("us" if 2 in unit_field else "ns")
                col = pd.to_datetime(col.astype("int64"), unit=unit)
            elif info["converted"] in (9, 10):  # TIMESTAMP_MILLIS / MICROS
                col = pd.to_datetime(col.astype("int64"), unit="ms" if info["converted"] == 9 else "us")
            frame[c] = col
    return pd.DataFrame(frame)
