"""Bounded CPU evidence; fixed cgroup paths expose mount-root counters, not a proven job scope."""
import os
import resource
import threading
import time


def _read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    failure = None
    try:
        info = os.fstat(descriptor)
        data = os.read(descriptor, 4097)
        if len(data) > 4096:
            raise ValueError('CPU evidence exceeded fixed bound')
        return data.decode('ascii'), [info.st_dev, info.st_ino]
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            os.close(descriptor)
        except BaseException as cleanup:
            if failure is not None:
                if (isinstance(failure, (TimeoutError, InterruptedError)) or not isinstance(failure, Exception)
                        or isinstance(cleanup, Exception) and not isinstance(cleanup, (TimeoutError, InterruptedError))):
                    raise failure from cleanup
            raise


def snapshot(*, cgroup=False):
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = {'pid': os.getpid(), 'tid': threading.get_native_id(),
              'monotonic_ns': time.monotonic_ns(), 'process_cpu_ns': time.process_time_ns(),
              'thread_cpu_ns': time.thread_time_ns(),
              'waited_children_cpu_us': [round(children.ru_utime * 1000000), round(children.ru_stime * 1000000)]}
    paths = {'thread_schedstat': '/proc/thread-self/schedstat'}
    if cgroup:
        paths.update(cpu_stat='/sys/fs/cgroup/cpu.stat', cpu_pressure='/sys/fs/cgroup/cpu.pressure',
                     cpu_max='/sys/fs/cgroup/cpu.max')
    for name, path in paths.items():
        try:
            text, identity = _read(path)
            result[name] = {'text': text, 'file_identity': identity}
        except (TimeoutError, InterruptedError):
            raise
        except (OSError, ValueError) as error:
            result[name] = {'unavailable': type(error).__name__}
    return result
