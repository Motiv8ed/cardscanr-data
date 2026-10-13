#!/usr/bin/env python3
from __future__ import annotations

import base64
import json

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

import pokecardex_zh_hans_acquire as p


def test_normalize_collector() -> None:
    assert p.normalize_collector("001/151") == "1"
    assert p.normalize_collector("032") == "32"


def test_image_url_standard_and_versioned() -> None:
    assert p.image_url("151C", 1).endswith("/sets_chn/151C/1.jpg?class=md")
    assert p.image_url("151C", 1, version_id=2).endswith("/sets_chn/151C/2/1.jpg?class=md")


def test_aes_roundtrip_helper() -> None:
    key = b"0123456789abcdef0123456789abcdef"
    iv = b"0123456789abcdef"
    payload = {"hello": "world", "n": 1}
    padder = PKCS7(128).padder()
    data = padder.update(json.dumps(payload).encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    enc = encryptor.update(data) + encryptor.finalize()
    blob = {"iv": base64.b64encode(iv).decode(), "data": base64.b64encode(enc).decode()}
    assert p.decrypt_initial_data(blob, key.decode()) == payload


def test_extract_encrypted_blob_json() -> None:
    html = 'window.__INITIAL_DATA_ALL_ENCRYPTED__ = {"iv":"YWI=","data":"Y2Q="};'
    assert p.extract_encrypted_blob(html) == {"iv": "YWI=", "data": "Y2Q="}
