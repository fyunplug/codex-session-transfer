"""Bounded, read-only access to plain and Zstandard-compressed rollout segments."""
import ctypes
import ctypes.util
from pathlib import Path

LIMIT = 256 * 1024 * 1024


def zstd_library():
    paths = [ctypes.util.find_library('zstd')]
    runtime = Path.home() / '.cache/codex-runtimes/codex-primary-runtime/dependencies/native'
    paths += [str(runtime / folder / 'libzstd.dll') for folder in
              ('poppler/Library/bin', 'git/mingw64/bin')]
    paths += [str(runtime / 'poppler/Library/bin/zstd.dll')]
    for path in paths:
        if not path:
            continue
        try:
            lib = ctypes.CDLL(path)
            lib.ZSTD_createDStream.restype = ctypes.c_void_p
            lib.ZSTD_initDStream.argtypes = [ctypes.c_void_p]
            lib.ZSTD_initDStream.restype = ctypes.c_size_t
            lib.ZSTD_freeDStream.argtypes = [ctypes.c_void_p]
            lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
            return lib
        except (OSError, AttributeError):
            continue
    raise ValueError('Compressed .jsonl.zst history requires Python 3.14+, the zstandard '
                     'module, or the Codex bundled libzstd runtime. No partial export was made.')


def chunks(path):
    path = Path(path)
    with path.open('rb') as source:
        if path.suffix != '.zst':
            while data := source.read(65536):
                yield data
            return
        try:
            from compression import zstd
        except ImportError:
            try:
                import zstandard as zstd
            except ImportError:
                zstd = None
        if zstd is not None:
            try:
                if hasattr(zstd, 'ZstdFile'):
                    reader = zstd.ZstdFile(source, 'rb')
                else:
                    reader = zstd.ZstdDecompressor().stream_reader(source)
                with reader:
                    while data := reader.read(65536):
                        yield data
            except Exception as exc:
                raise ValueError('Invalid compressed session: ' + str(path)) from exc
            return
        lib = zstd_library()

        class Buffer(ctypes.Structure):
            _fields_ = [('data', ctypes.c_void_p), ('size', ctypes.c_size_t), ('pos', ctypes.c_size_t)]

        lib.ZSTD_decompressStream.argtypes = [ctypes.c_void_p, ctypes.POINTER(Buffer), ctypes.POINTER(Buffer)]
        lib.ZSTD_decompressStream.restype = ctypes.c_size_t
        stream = lib.ZSTD_createDStream()
        if not stream:
            raise ValueError('Cannot allocate Zstandard reader.')
        try:
            result = lib.ZSTD_initDStream(stream)
            if lib.ZSTD_isError(result):
                raise ValueError('Cannot initialize Zstandard reader.')
            while data := source.read(65536):
                incoming = ctypes.create_string_buffer(data)
                inp = Buffer(ctypes.cast(incoming, ctypes.c_void_p), len(data), 0)
                full_output = True
                while inp.pos < inp.size or full_output:
                    output = ctypes.create_string_buffer(65536)
                    out = Buffer(ctypes.cast(output, ctypes.c_void_p), len(output), 0)
                    previous = inp.pos
                    result = lib.ZSTD_decompressStream(stream, ctypes.byref(out), ctypes.byref(inp))
                    if lib.ZSTD_isError(result):
                        raise ValueError('Invalid Zstandard session: ' + str(path))
                    if out.pos:
                        yield output.raw[:out.pos]
                    full_output = out.pos == out.size and result != 0
                    if inp.pos == previous and not out.pos:
                        if inp.pos == inp.size:
                            break
                        raise ValueError('Zstandard reader made no progress.')
            if result != 0:
                raise ValueError('Truncated Zstandard session: ' + str(path))
        finally:
            lib.ZSTD_freeDStream(stream)


def read_session(path, prefix=None):
    result = bytearray()
    iterator = chunks(path)
    try:
        for data in iterator:
            if prefix is not None:
                data = data[:prefix - len(result)]
            result.extend(data)
            if len(result) > LIMIT:
                raise ValueError('Decoded history exceeds 256 MiB; no partial archive produced.')
            if prefix is not None and len(result) == prefix:
                break
    finally:
        iterator.close()
    return bytes(result)


def first_line(path):
    result = bytearray()
    iterator = chunks(path)
    try:
        for data in iterator:
            result.extend(data.split(b'\n', 1)[0])
            if len(result) > 4 * 1024 * 1024:
                raise ValueError('Session header exceeds 4 MiB.')
            if b'\n' in data:
                return bytes(result)
    finally:
        iterator.close()
    return bytes(result)
