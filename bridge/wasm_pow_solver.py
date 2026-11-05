"""
wasm_pow_solver.py — Uses the real DeepSeek WASM to solve POW challenges.

This guarantees 100% compatibility with the server's verifier because it runs
the exact same hash logic.  Falls back to the pure-Python solver if wasmtime
is unavailable.
"""

import base64
import json
import os
import struct
import threading

_WASM_FILE = os.path.join(os.path.dirname(__file__), "sha3_wasm_bg.wasm")

# Cache the WASM instance (thread-local is safest; wasmtime Store is not thread-safe)
_local = threading.local()


def _get_wasm():
    """Return (wasm_instance, store) from thread-local cache."""
    if hasattr(_local, "wasm"):
        return _local.wasm, _local.store

    from wasmtime import Engine, FuncType, Linker, Module, Store, ValType

    with open(_WASM_FILE, "rb") as f:
        wasm_bytes = f.read()

    engine = Engine()
    store = Store(engine)
    module = Module(engine, wasm_bytes)
    linker = Linker(engine)

    # The only import the WASM needs is wbg.__wbindgen_throw
    def _throw(caller, ptr, length):
        mem = caller.get("memory")
        if mem:
            data = bytes(mem.data_ptr(store)[ptr: ptr + length])
            raise RuntimeError(f"WASM error: {data.decode(errors='replace')}")
        raise RuntimeError("WASM threw an error")

    throw_type = FuncType([ValType.i32(), ValType.i32()], [])
    linker.define_func("wbg", "__wbindgen_throw", throw_type, _throw)

    instance = linker.instantiate(store, module)
    _local.wasm = instance
    _local.store = store
    return instance, store


def solve_wasm(challenge: str, salt: str, expire_at: int, difficulty: int) -> int:
    """
    Solve the DeepSeek POW challenge using the real WASM.

    Returns the integer nonce.
    """
    instance, store = _get_wasm()

    mem = instance.exports(store)["memory"]
    add_to_stack = instance.exports(store)["__wbindgen_add_to_stack_pointer"]
    alloc = instance.exports(store)["__wbindgen_export_0"]   # wasm malloc
    wasm_solve = instance.exports(store)["wasm_solve"]

    prefix = f"{salt}_{expire_at}_"

    def write_str(s: str) -> tuple[int, int]:
        buf = s.encode("utf-8")
        # wasmtime returns a plain int from single-return functions
        ptr = alloc(store, len(buf) + 1, 1)
        import ctypes as _ct
        raw_ptr = _ct.cast(mem.data_ptr(store), _ct.POINTER(_ct.c_uint8))
        for i, b in enumerate(buf):
            raw_ptr[ptr + i] = b
        return ptr, len(buf)

    # Allocate return-value area on WASM stack (16 bytes)
    retptr = add_to_stack(store, -16)

    try:
        chal_ptr, chal_len = write_str(challenge)
        pre_ptr, pre_len = write_str(prefix)

        wasm_solve(store, retptr, chal_ptr, chal_len, pre_ptr, pre_len, float(difficulty))

        # Read result: i32 at retptr+0, f64 at retptr+8
        import ctypes as _ct2
        raw_ptr = _ct2.cast(mem.data_ptr(store), _ct2.POINTER(_ct2.c_uint8))
        raw = bytes(raw_ptr[retptr + i] for i in range(16))
        found = struct.unpack_from("<i", raw, 0)[0]
        nonce_f = struct.unpack_from("<d", raw, 8)[0]
    finally:
        add_to_stack(store, 16)  # restore stack

    if not found:
        raise RuntimeError("WASM wasm_solve: no nonce found within search range")

    return int(nonce_f)


def build_pow_response(challenge_data: dict, nonce: int) -> str:
    """
    Build the base64-encoded JSON that goes in the x-ds-pow-response header.
    Uses only the 6 fields the browser sends (extra fields cause rejection).
    """
    payload = {
        "algorithm":  challenge_data["algorithm"],
        "challenge":  challenge_data["challenge"],
        "salt":       challenge_data["salt"],
        "answer":     nonce,
        "signature":  challenge_data["signature"],
        "target_path": challenge_data["target_path"],
    }
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
