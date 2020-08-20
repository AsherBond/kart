import base64
import hashlib
import json

import msgpack


def msg_pack(data):
    """data (any type) -> bytes"""
    return msgpack.packb(data, use_bin_type=True)


def msg_unpack(bytestring_or_memoryview):
    """bytes/memoryview -> data (any type)"""
    return msgpack.unpackb(bytestring_or_memoryview, raw=False)


# json_pack and json_unpack have the same signature and capabilities as msg_pack and msg_unpack,
# but their storage format is less compact and more human-readable.
def json_pack(data):
    """data (any type) -> bytes"""
    return json.dumps(data).encode("utf8")


def json_unpack(bytestring):
    """bytes -> data (any type)"""
    return json.loads(bytestring, encoding="utf8")


def b64encode_str(bytestring):
    """bytes -> urlsafe str"""
    return base64.urlsafe_b64encode(bytestring).decode("ascii")


def b64decode_str(b64_str):
    """urlsafe str -> bytes"""
    return base64.urlsafe_b64decode(b64_str)


def sha256(*data):
    """*data (str or bytes) -> sha256. Irreversible."""
    h = hashlib.sha256()
    for d in data:
        h.update(ensure_bytes(d))
    return h


def hexhash(*data):
    """*data (str or bytes) -> hex str. Irreversible."""
    # We only return 160 bits of the hash, same as git hashes - more is overkill.
    return sha256(*data).hexdigest()[:40]


def ensure_bytes(data):
    """data (str or bytes) -> bytes. Utf-8."""
    if isinstance(data, str):
        return data.encode('utf8')
    return data


def ensure_text(data):
    """data (str or bytes) -> str. Utf-8."""
    if isinstance(data, bytes):
        return data.decode('utf8')
    return data