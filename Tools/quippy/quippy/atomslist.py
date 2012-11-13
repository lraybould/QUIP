# HQ XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX
# HQ X
# HQ X   quippy: Python interface to QUIP atomistic simulation library
# HQ X
# HQ X   Copyright James Kermode 2010
# HQ X
# HQ X   These portions of the source code are released under the GNU General
# HQ X   Public License, version 2, http://www.gnu.org/copyleft/gpl.html
# HQ X
# HQ X   If you would like to license the source code under different terms,
# HQ X   please contact James Kermode, james.kermode@gmail.com
# HQ X
# HQ X   When using this software, please cite the following reference:
# HQ X
# HQ X   http://www.jrkermode.co.uk/quippy
# HQ X
# HQ XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX

import sys, os, fnmatch, re, itertools, glob, operator, warnings, math, logging
from quippy.atoms import Atoms, AtomsReaders, AtomsWriters, atoms_reader
from quippy.system import mem_info
from quippy.util import infer_format, parse_slice, time_ordered_glob
from quippy.mockndarray import mockNDarray
from quippy.farray import *
import numpy as np

__all__ = ['AtomsReader', 'AtomsWriter', 'AtomsList', 'read_dataset', 'time_ordered_series']

class AtomsReaderMixin(object):
    def __repr__(self):
        try:
            len_self = len(self)
        except:
            len_self = None
        return '<%s source=%r format=%r start=%r stop=%r step=%r random_access=%r len=%r>' % \
               (self.__class__.__name__, self.source, self.format, self._start, self._stop, self._step,
                self.random_access, len_self)

    def write(self, dest=None, format=None, properties=None, prefix=None,
              progress=False, progress_width=80, update_interval=None,
              show_value=True, **kwargs):
        opened = False
        if dest is None:
            dest = self.source
        filename, dest, format = infer_format(dest, format, AtomsWriters)

        if progress:
            from progbar import ProgressBar
            pb = ProgressBar(0,len(self),progress_width,showValue=show_value)
            update_interval = update_interval or max(1, len(self)/progress_width)

        if format in AtomsWriters:
            dest = AtomsWriters[format](dest, **kwargs)

        if not hasattr(dest, 'write'):
            raise ValueError("Don't know how to write to destination \"%s\" in format \"%s\"" % (dest, format))

        res = []
        for i, a in enumerate(self):
            write_kwargs = {}
            if properties is not None: write_kwargs['properties'] = properties
            if prefix is not None: write_kwargs['prefix'] = prefix
            try:
                res.append(dest.write(a, **write_kwargs))
            except TypeError:
                raise ValueError('destination does not support specifying arguments %r' % write_kwargs)

            if progress and i % update_interval == 0: pb(i)

        if opened:
            dest.close()

        # Special case for writing to a string
        if format == 'string':
            return ''.join(res)
        else:
            if res is not None and not all(el is None for el in res):
                return res

class AtomsReader(AtomsReaderMixin):
    """Class to read Atoms frames from source"""

    def __init__(self, source, format=None, start=None, stop=None, step=None,
                 cache_mem_limit=-1, **kwargs):

        def file_exists(f):
            return f == "stdin" or os.path.exists(f) or len(glob.glob(f)) > 0

        self.source = source
        self.format = format
        self._start = start
        self._stop = stop
        self._step = step

        self.cache_mem_limit = cache_mem_limit
        logging.debug('AtomsReader memory limit %r' % self.cache_mem_limit)

        self._source_len = None
        self._cache_dict = {}
        self._cache_list  = []
        self._cache_mem_usage = []

        self.opened = False
        self.reader = source

        if isinstance(self.reader, basestring):
            if '@' in self.reader:
                self.reader, frames = self.reader.split('@')
                frames = parse_slice(frames)
                if start is not None or stop is not None or step is not None:
                    raise ValueError('Conflicting frame references start=%r stop=%r step=%r and @-sytnax %r' %
                                     (start, stop, step, frames))
                if isinstance(frames, int):
                    if frames >= 0:
                        frames = slice(frames, frames+1,+1)
                    else:
                        frames = slice(frames, frames-1,-1)

                self._start, self._stop, self._step = frames.start, frames.stop, frames.step
                
            self.filename = self.reader
            self.opened = True
            if self.reader in AtomsReaders:
                if format is None:
                    format = self.reader
            elif format != 'string':
                self.reader = os.path.expanduser(self.reader)
                glob_list = sorted(glob.glob(self.reader))
                if (len(glob_list) == 0):
                    raise IOError("input file '%s' not found" % self.reader)
                if len(glob_list) > 1:
                    self.reader = glob_list
                else:
                    self.reader = glob_list[0]
                    filename, self.reader, new_format = infer_format(self.reader, format, AtomsReaders)
                    
                    if format is None:
                        format = new_format

        # special cases if source is a list or tuple of filenames or Atoms objects
        is_filename_sequence = False
        is_list_of_atoms = False
        if isinstance(self.reader, list) or isinstance(self.reader, tuple):
            is_filename_sequence = True
            is_list_of_atoms = True
            for item in self.reader:
                if '@' in item:
                    item = item[:item.index('@')]
                if not isinstance(item, basestring) or not file_exists(item):
                    is_filename_sequence = False
                if not isinstance(item, Atoms):
                    is_list_of_atoms = False

        if is_filename_sequence:
            self.reader = AtomsSequenceReader(self.reader, format=format, **kwargs)
        elif is_list_of_atoms:
            # dummy reader which copies from an existing list or tuple of Atoms objects
            self.reader = [ at.copy() for at in self.reader ]
        else:
            if format is None:
                format = self.reader.__class__
            if format in AtomsReaders:
                self.reader = AtomsReaders[format](self.reader, **kwargs)

        if isinstance(self.reader, basestring):
            raise IOError("Don't know how to read Atoms from file '%s'" % self.reader)

        if isinstance(self.reader, AtomsReader):
            self.reader = AtomsReaderCopier(self.reader)

        if not hasattr(self.reader, '__iter__'):
            # call Atoms constructor - this has beneficial side effect of making a copy
            self.reader = [Atoms(self.reader)]

    def __len__(self):
        if self._source_len is not None:
            return len(range(*slice(self._start, self._stop, self._step).indices(self._source_len)))
        elif hasattr(self.reader, '__len__') and hasattr(self.reader, '__getitem__'):
            try:
                return len(range(*slice(self._start, self._stop, self._step).indices(len(self.reader))))
            except:
                raise AttributeError('This AtomsReader does not support random access')
        else:
            raise AttributeError('This AtomsReader does not support random access')

    @property
    def random_access(self):
        try:
            len(self)
            return True
        except:
            return False

    def close(self):
        if self.opened and hasattr(self.reader, 'close'):
            self.reader.close()

    def __getslice__(self, first, last):
        return self.__getitem__(slice(first,last,None))

    def _cache_fetch(self, frame):
        # least recently used (LRU) cache
        try:
            self._cache_list.append(self._cache_list.pop(self._cache_list.index(frame)))
            return True   # cache hit
        except ValueError:
            return False  # cache miss

    def _cache_store(self, frame, at):
        self._cache_list.append(frame)
        self._cache_dict[frame] = at

        if self.cache_mem_limit is not None:
            if self.cache_mem_limit == -1:
                self.cache_mem_limit = min(10*at.mem_estimate(), 100*1024**2)
            
            if self.cache_mem_limit == 0:
                while len(self._cache_dict) > 1:
                    logging.debug('Reducing AtomsReader cache size from %d' % len(self._cache_dict))
                    del self._cache_dict[self._cache_list.pop(0)]
            else:
                self._cache_mem_usage.append(at.mem_estimate())
                while len(self._cache_dict) > 1 and sum(self._cache_mem_usage) > self.cache_mem_limit:
                    logging.debug('Reducing AtomsReader cache size from %d' % len(self._cache_dict))
                    self._cache_mem_usage.pop(0)
                    del self._cache_dict[self._cache_list.pop(0)]

    def __getitem__(self, frame):
        if not self.random_access:
            raise IndexError('This AtomsReader does not support random access')

        if isinstance(frame, int) or isinstance(frame, np.integer):
            source_len = self._source_len or len(self.reader)
            if self._start is not None or self._stop is not None or self._step is not None:
                frame = range(*slice(self._start, self._stop, self._step).indices(source_len))[frame]
            if frame < 0: frame = frame + len(self)

            if not self._cache_fetch(frame):
                self._cache_store(frame, self.reader[frame])

            at = self._cache_dict[frame]
            if not hasattr(at, 'source'):
                at.source = self.source
            if not hasattr(at, 'frame'):
                at.frame = frame
            return at

        elif isinstance(frame, slice):
            return self.__class__([self[f] for f in range(*frame.indices(len(self))) ])
        else:
            raise TypeError('frame should be either an integer or a slice')

    def __setitem__(self, frame, at):
        self._cache_store(frame, at)

    def iterframes(self, reverse=False):
        if self.random_access:
            # iterate using __getitem__, which automatically goes through LRU cache

            frames = range(len(self))
            if reverse: frames = reversed(frames)
            for f in frames:
                yield self[f]

        else:
            # source does not support random access
            # for each call, we make a new iterator from self.reader

            if reverse:
                raise IndexError('Cannot reverse iterate over an AtomsReader which does not support random access')

            frames = itertools.count()
            atoms = iter(self.reader)

            if self._start is not None or self._stop is not None or self._step is not None:
                frames = itertools.islice(frames, self._start or 0, self._stop or None, self._step or 1)
                atoms  = itertools.islice(atoms, self._start or 0, self._stop or None, self._step or 1)

            n_frames = 0
            last_frame = 0
            for (frame,at) in itertools.izip(frames, atoms):
                self._cache_store(frame, at)
                n_frames += 1
                last_frame = frame
                if not hasattr(at, 'source'):
                    at.source = self.source
                if not hasattr(at, 'frame'):
                    at.frame = frame
                yield at

            # once iteration is finished, random access will be possible if all frames fitted inside cache
            if len(self._cache_dict) == n_frames:
                self.reader = self._cache_dict
                self._source_len = last_frame+1

    def __iter__(self):
        return self.iterframes()

    def __reversed__(self):
        return self.iterframes(reverse=True)


class AtomsList(AtomsReaderMixin, list):
    def __init__(self, source=[], format=None, start=None, stop=None, step=None, **kwargs):
        self.source = source
        self.format = format
        self._start  = start
        self._stop   = stop
        self._step   = step
        tmp_ar = AtomsReader(source, format, start, stop, step, **kwargs)
        list.__init__(self, list(iter(tmp_ar)))
        tmp_ar.close()

    def __getattr__(self, name):
        if name.startswith('__'):
            # don't override any special attributes
            raise AttributeError

        try:
            return self.source.__getattr__(name)
        except AttributeError:
            try:
                seq = [getattr(at, name) for at in iter(self)]
            except AttributeError:
                raise
            if seq == []:
                return None
            elif type(seq[0]) in (FortranArray, np.ndarray):
                return mockNDarray(*seq)
            else:
                return seq

    def __getslice__(self, first, last):
        return self.__getitem__(slice(first,last,None))

    def __getitem__(self, idx):
        if isinstance(idx, list) or isinstance(idx, np.ndarray):
            idx = np.array(idx)
            if idx.dtype.kind not in ('b', 'i'):
                raise IndexError("Array used for fancy indexing must be of type integer or bool")
            if idx.dtype.kind == 'b':
                idx = idx.nonzero()[0]
            res = []
            for i in idx:
                at = list.__getitem__(self,i)
                res.append(at)
        else:
            res = list.__getitem__(self, idx)
        if isinstance(res, list):
            res = AtomsList(res)
        return res

    def iterframes(self, reverse=False):
        if reverse:
            return reversed(self)
        else:
            return iter(self)

    @property
    def random_access(self):
        return True

    def sort(self, cmp=None, key=None, reverse=False, attr=None):
        if attr is None:
            list.sort(self, cmp, key, reverse)
        else:
            if cmp is not None or key is not None:
                raise ValueError('If attr is present, cmp and key must not be present')
            list.sort(self, key=operator.attrgetter(attr), reverse=reverse)

    def apply(self, func):
        return np.array([func(at) for at in self])
        

def AtomsWriter(dest, format=None, **kwargs):
    """Return a file-like object capable of writing Atoms in the specified format.
       If `format` is not given it is inferred from the file extension of `dest`."""

    filename, dest, format = infer_format(dest, format, AtomsWriters)
    if format in AtomsWriters:
        return AtomsWriters[format](dest, **kwargs)
    else:
        raise ValueError("Don't know how to write Atoms to format %r" % format)



class AtomsSequenceReader(object):
    """Read Atoms from a list of sources"""

    def __init__(self, sources, **kwargs):
        self.sources = sources
        self.readers = []
        self.lengths = []
        for source in sources:
            reader = AtomsReader(source, **kwargs)
            self.readers.append(reader)
            try:
                self.lengths.append(len(reader))
            except AttributeError:
                self.lengths.append(None)

    def __len__(self):
        if None in self.lengths:
            raise IndexError('One or more sources in %r do not support random access' % self.sources)
        return sum(self.lengths)

    def __getitem__(self, index):
        if None in self.lengths:
            raise IndexError('One or more sources in %r do not support random access' % self.sources)

        if isinstance(index,int) or isinstance(index, np.integer):
            if index < 0: index = index + sum(self.lengths)

            idx = 0
            for len, reader, source in zip(self.lengths, self.readers, self.sources):
                if index >= idx and index < idx+len:
                    at = reader[index-idx]
                    at.source = source 
                    return at
                idx = idx+len

            raise IndexError('index %d out of range 0..%d' % (index, sum(self.lengths)))

        elif isinstance(index,slice):
            return [self[f] for f in range(*index.indices(sum(self.lengths)))]
        else:
            raise IndexError('indexing object should be either int or slice')


    def __iter__(self):
        for source, reader in zip(self.sources, self.readers):
            for at in reader:
                at.source = source
                yield at


class AtomsReaderCopier(object):
    def __init__(self, source):
        self.source = source

    def __len__(self):
        return len(self.source)

    def __iter__(self):
        for item in self.source:
            yield item.copy()

    def __getitem__(self, index):
        if isinstance(index, int) or isinstance(index, np.integer):
            return self.source[index].copy()
        elif isinstance(index, slice):
            return [at.copy() for at in self.source[index]]
        else:
            raise IndexError('indexing object should be either int or slice')            

    

def read_dataset(dirs, pattern, **kwargs):
    """
    Read atomic configurations matching glob `pattern` from each of
    the directories in `dir` in turn. All kwargs are passed along
    to AtomsList constructor.

    Returns an dictionary mapping directories to AtomsList instances.
    """
    dataset = {}
    for dir in dirs:
        dataset[dir] = AtomsList(os.path.join(dir, pattern), **kwargs)
    return dataset


def time_ordered_series(source, dt=None):
    """
    Given a source of Atoms configurations, return a time ordered list of filename and frame references
    """

    if not isinstance(source, AtomsReader):
        if isinstance(source, basestring):
            source = time_ordered_glob(source)
        source = AtomsReader(source, range='empty')

    # traverse backwards over source, choosing most recently modified version of each frame
    revsource = reversed(source)
    last = revsource.next()
    current_time = last.time
    revorder = [(last.source, last.frame)]
    for at in revsource:
        try:
            if (at.time >= current_time) or (dt is not None and current_time - at.time < dt):
                continue
        except AttributeError:
            continue
        current_time = at.time
        revorder.append((at.source, at.frame))

    filenames = []
    # group first by filename
    for key, group in itertools.groupby(reversed(revorder), lambda x: x[0]):
        # annotate group with stride between frames
        group_with_stride = []
        group = list(group)
        for (s1, f1), (s2, f2) in zip(group, group[1:]):
            stride = f2 - f1
            group_with_stride.append((s1, f1, stride))
        group_with_stride.append(group[-1] + (stride,))

        # now group again by stride
        for key, group in itertools.groupby(group_with_stride, lambda x: x[2]):
            group = list(group)
            filenames.append('%s@%d:%d:%d' % (group[0][0], group[0][1], group[-1][1], group[0][2]))

    return filenames
    
            

    

        
    
