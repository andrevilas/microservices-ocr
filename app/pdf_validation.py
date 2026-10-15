"""Resource-limited child process; input is an internal staged path, never a URL."""
import json
import math
import os
import sys


def limit_memory(size):
    if os.name != "nt":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (size, size))
        return None
    import ctypes as c
    from ctypes import wintypes as w

    class Basic(c.Structure):
        _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64), ("flags", w.DWORD),
                    ("minimum_ws", c.c_size_t), ("maximum_ws", c.c_size_t), ("active", w.DWORD),
                    ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

    class IO(c.Structure):
        _fields_ = [(name, c.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

    class Extended(c.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                    ("peak_process", c.c_size_t), ("peak_job", c.c_size_t)]

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [c.c_void_p, w.LPCWSTR], w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
    kernel.SetInformationJobObject.restype = w.BOOL
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.AssignProcessToJobObject.restype = w.BOOL
    handle = kernel.CreateJobObjectW(None, None)
    limits = Extended()
    limits.basic.flags, limits.process_memory = 0x100, size  # JOB_OBJECT_LIMIT_PROCESS_MEMORY
    if not handle or not kernel.SetInformationJobObject(handle, 9, c.byref(limits), c.sizeof(limits)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise OSError("Could not install process memory limit")
    return handle  # Retained until process exit.


def main():
    path, memory, pages_limit, pixels_limit = sys.argv[1:]
    try:
        memory_handle = limit_memory(int(memory))
    except Exception:
        print(json.dumps({"error": "validation_unavailable"}))
        return
    try:
        from pypdf import PdfReader
        with open(path, "rb") as stream:
            if stream.read(5) != b"%PDF-":
                raise ValueError()
            stream.seek(0)
            reader = PdfReader(stream)
            if reader.is_encrypted:
                raise ValueError()
            count = len(reader.pages)
            if not 1 <= count <= int(pages_limit):
                print(json.dumps({"error": "page_limit_exceeded"}))
                return
            for page in reader.pages:
                scale = float(page.get("/UserUnit", 1)) * 300 / 72
                width, height = float(page.mediabox.width) * scale, float(page.mediabox.height) * scale
                if not all(math.isfinite(n) and n > 0 for n in (width, height)) or width * height > int(pixels_limit):
                    print(json.dumps({"error": "page_pixels_exceeded"}))
                    return
        print(json.dumps({"pages": count}))
    except Exception:
        print(json.dumps({"error": "invalid_pdf"}))


if __name__ == "__main__":
    main()
