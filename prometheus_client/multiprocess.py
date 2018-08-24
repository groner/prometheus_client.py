#!/usr/bin/python

from __future__ import unicode_literals

from collections import defaultdict
import contextlib
import errno
import fcntl
import glob
import json
import os

from . import core


class MultiProcessCollector(object):
    """Collector for files for multi-process mode."""
    def __init__(self, registry, path=None):
        if path is None:
            path = os.environ.get('prometheus_multiproc_dir')
        if not path or not os.path.isdir(path):
            raise ValueError('env prometheus_multiproc_dir is not set or not a directory')
        self._path = path
        if registry:
            registry.register(self)

    def collect(self):
        metrics = {}
        # Lock to avoid racing with a compacting operation.
        with flocking(os.path.join(self._path, 'compacting'), fcntl.LOCK_SH):
            for f in glob.glob(os.path.join(self._path, '*.db')):
                parts = os.path.basename(f).split('_')
                typ = parts[0]

                d = core._MmapedDict(f, read_mode=True)
                for key, value in d.read_all_values():
                    metric_name, name, labelnames, labelvalues = json.loads(key)

                    metric = metrics.get(metric_name)
                    if metric is None:
                        metric = core.Metric(metric_name, 'Multiprocess metric', typ)
                        metrics[metric_name] = metric

                    if typ == 'gauge' and parts[2] != 'archived.db':
                        pid = parts[2]
                        metric._multiprocess_mode = parts[1]
                        metric.add_sample(name, tuple(zip(labelnames, labelvalues)) + (('pid', pid), ), value)
                    else:
                        # The duplicates and labels are fixed in the next for.
                        metric.add_sample(name, tuple(zip(labelnames, labelvalues)), value)
                d.close()

        for metric in metrics.values():
            samples = defaultdict(float)
            buckets = {}
            for name, labels, value in metric.samples:
                if metric.type == 'gauge':
                    without_pid = tuple(l for l in labels if l[0] != 'pid')
                    if metric._multiprocess_mode == 'min':
                        current = samples.setdefault((name, without_pid), value)
                        if value < current:
                            samples[(name, without_pid)] = value
                    elif metric._multiprocess_mode == 'max':
                        current = samples.setdefault((name, without_pid), value)
                        if value > current:
                            samples[(name, without_pid)] = value
                    elif metric._multiprocess_mode == 'livesum':
                        samples[(name, without_pid)] += value
                    else:  # all/liveall
                        samples[(name, labels)] = value

                elif metric.type == 'histogram':
                    bucket = tuple(float(l[1]) for l in labels if l[0] == 'le')
                    if bucket:
                        # _bucket
                        without_le = tuple(l for l in labels if l[0] != 'le')
                        buckets.setdefault(without_le, {})
                        buckets[without_le].setdefault(bucket[0], 0.0)
                        buckets[without_le][bucket[0]] += value
                    else:
                        # _sum/_count
                        samples[(name, labels)] += value

                else:
                    # Counter and Summary.
                    samples[(name, labels)] += value

            # Accumulate bucket values.
            if metric.type == 'histogram':
                for labels, values in buckets.items():
                    acc = 0.0
                    for bucket, value in sorted(values.items()):
                        acc += value
                        samples[(metric.name + '_bucket', labels + (('le', core._floatToGoString(bucket)), ))] = acc
                    samples[(metric.name + '_count', labels)] = acc

            # Convert to correct sample format.
            metric.samples = [(name, dict(labels), value) for (name, labels), value in samples.items()]
        return metrics.values()


def mark_process_dead(pid, path=None):
    """Do bookkeeping for when one process dies in a multi-process setup."""
    if path is None:
        path = os.environ.get('prometheus_multiproc_dir')
    for f in glob.glob(os.path.join(path, '*_{0}_*.db'.format(pid))):
        with open(f, 'rb') as lfh:
            try:
                fcntl.flock(lfh.fileno(), fcntl.LOCK_EX|fcntl.LOCK_NB)
            #except BlockingIOError:
            except IOError as err:
                if err.errno != errno.EWOULDBLOCK:
                    raise
                # The file is in use, we're either seeing pid reuse or perhaps
                # the fd/lock was leaked.
                continue
        fn = os.path.basename(f)
        if fn.startswith('gauge_live'):
            os.remove(f)
        else:
            compact(f)


def compact(srcf):
    path = os.environ.get('prometheus_multiproc_dir')

    # Lock to avoid compacting while MultiProcessCollector is reading.
    with flocking(os.path.join(path, 'compacting'), fcntl.LOCK_EX):
        parts = os.path.basename(srcf).split('_')
        typ = parts[0]
        mm = parts[1] if typ == 'gauge' else None
        pid = parts[2] if typ == 'gauge' else parts[1]

        if mm:
            dstf = os.path.join(path, '{0}_{1}_archived.db'.format(typ, mm))
        else:
            dstf = os.path.join(path, '{0}_archived.db'.format(typ))

        src = core._MmapedDict(srcf, read_mode=True)
        dst = core._MmapedDict(dstf)

        for key, value in src.read_all_values():
            if typ == 'gauge':
                if mm == 'min':
                    vnow = dst.read_value(key, init=False)
                    if vnow is None or value < vnow:
                        dst.write_value(key, value)

                elif mm == 'max' and value > dst.read_value(key):
                    vnow = dst.read_value(key, init=False)
                    if vnow is None or value > vnow:
                        dst.write_value(key, value)

                elif mm == 'all':
                    metric_name, name, labelnames, labelvalues = json.loads(key)
                    key = json.dumps((metric_name, name, labelnames+['pid'], labelvalues+[pid]))
                    dst.write_value(key, value)

            else:
                dst.write_value(key, dst.read_value(key)+value)

        os.unlink(srcf)
        dst.close()
        src.close()


@contextlib.contextmanager
def flocking(fn, op):
    with open(fn, 'ab+') as fh:
        fcntl.flock(fh.fileno(), op)
        yield
