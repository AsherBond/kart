from collections import UserDict
from dataclasses import dataclass
from itertools import chain
from typing import Any, Iterable, Iterator

from .exceptions import InvalidOperation


# A pseudo "dataset-path" used as the key for storing files. Files are all individual top-level items in the repo,
# not pieces of a dataset - and so are addressed like this "path/to/file.txt" rather than something like this
# "<dataset-path>:feature:<primary-key>". They may be alongside dataset content (ie <dataset-path>/my-attachment.txt)
# or not (ie <repo-root>/my-attachment.txt) but when outputting diffs, they all go in the <files> area and not in any dataset.
# Note that this is not a valid dataset-path, so it cannot conflict with any dataset.
FILES_KEY = "<files>"


class Conflict(Exception):
    pass


@dataclass
class KeyValue:
    """
    A key-value pair. A delta is made of two of these - one old, one new.
    """

    key: Any
    value: Any

    @staticmethod
    def of(obj):
        """Ensures that the given object is a KeyValue, or None."""
        if isinstance(obj, (KeyValue, type(None))):
            return obj
        elif isinstance(obj, tuple):
            return KeyValue(*obj)
        raise ValueError(f"Expected (key, value) tuple - got f{type(obj)}")

    def get_lazy_value(self):
        """Deltas can be created so that the values are only generated when needed."""

        if not callable(self.value):
            # Not a lazily evaluated value. Just a value.
            return self.value

        if not hasattr(self, "_cached_value"):
            # Time to evaluate value().
            self._cached_value = self.value()
        return self._cached_value


# Delta flags:
WORKING_COPY_EDIT = 0x1  # Delta represents a change made in the WC - it is "dirty".
BINARY_FILE = 0x2  # Delta is a change to a binary file.


@dataclass
class Delta:
    """
    An object changes from old to new. Either old or new can be None, for insert or delete operations.
    When present, old and new are both key-value pairs.
    The key identifies which object changed (so, should be a filename / address / primary key),
    and the value is the changed object's entire contents.
    If the old_key is different to the new_key, this means the object moved in this delta, ie a rename operation.
    Deltas can be concatenated together, if they refer to the same object - eg an delete + insert = update (usually).
    Deltas can be inverted, which just means old and new are swapped.

    Prefer to access (old_key, new_key, old_value, new_value) over (old.key, new.key, old.value, new.value)
    - these handle one possible AttributeError if old or new is None.
    - these handle the case where old.value or new.value is a callable and can be lazily evaluated.
    """

    old: KeyValue
    new: KeyValue

    def __init__(self, old, new):
        self.old = KeyValue.of(old)
        self.new = KeyValue.of(new)
        if old is None and new is None:
            raise ValueError("Empty Delta")
        elif old is None:
            self.type = "insert"
        elif new is None:
            self.type = "delete"
        else:
            self.type = "update"
        self.flags = 0

    @staticmethod
    def insert(new):
        return Delta(None, new)

    @staticmethod
    def update(old, new):
        return Delta(old, new)

    @staticmethod
    def maybe_update(old, new):
        return Delta(old, new) if old.get_lazy_value() != new.get_lazy_value() else None

    @staticmethod
    def delete(old):
        return Delta(old, None)

    @staticmethod
    def from_key_and_plus_minus_dict(key, d):
        if "--" in d:
            return Delta.delete((key, d["++"]))
        elif "++" in d:
            return Delta.insert((key, d["++"]))
        else:
            return Delta(
                (key, d["-"]) if "-" in d else None,
                (key, d["+"]) if "+" in d else None,
            )

    def __invert__(self):
        return Delta(self.new, self.old)

    @property
    def old_key(self):
        return self.old.key if self.old is not None else None

    @property
    def new_key(self):
        return self.new.key if self.new is not None else None

    def is_rename(self):
        return self.type == "update" and self.old_key != self.new_key

    @property
    def old_value(self):
        if self.old is not None:
            return self.old.get_lazy_value()
        return None

    @property
    def new_value(self):
        if self.new is not None:
            return self.new.get_lazy_value()
        return None

    @property
    def key(self):
        # To be stored in a Diff, a Delta needs a single key.
        # This mostly works, but isn't perfect when renames are involved.
        return self.old_key if self.old_key is not None else self.new_key

    def __add__(self, other: "Delta"):
        """Concatenate this delta with the subsequent delta, return the result as a single delta."""
        # Note: this method assumes that the deltas being concatenated are related,
        # ie that self.new == other.old. Don't try to concatenate arbitrary deltas together.

        if self.type == "insert":
            # ins + ins -> Conflict
            # ins + upd -> ins
            # ins + del -> noop
            if other.type == "insert":
                raise Conflict()
            elif other.type == "update":
                result = Delta.insert(other.new)
            elif other.type == "delete":
                result = None

        elif self.type == "update":
            # upd + ins -> Conflict
            # upd + upd -> upd?
            # upd + del -> del
            if other.type == "insert":
                raise Conflict()
            elif other.type == "update":
                result = Delta.maybe_update(self.old, other.new)
            elif other.type == "delete":
                result = Delta.delete(self.old)

        elif self.type == "delete":
            # del + ins -> upd?
            # del + del -> Conflict
            # del + upd -> Conflict
            if other.type == "insert":
                result = Delta.maybe_update(self.old, other.new)
            else:
                raise Conflict()

        if result is not None:
            result.flags = self.flags | other.flags
        return result

    def to_plus_minus_dict(self, delta_filter=None):
        from .key_filters import DeltaFilter

        if delta_filter is None:
            return self.to_plus_minus_dict__simple()
        else:
            return self.to_plus_minus_dict__advanced(delta_filter)

    def to_plus_minus_dict__simple(self, minimal=False):
        # Simplest behaviour - minus means old value, plus means new value.
        # Not configurable.
        if minimal and self.old and self.new:
            return {"*": self.new_value}
        result = {}
        if self.old:
            result["-"] = self.old_value
        if self.new:
            result["+"] = self.new_value
        return result

    def to_plus_minus_dict__advanced(self, delta_filter=None):
        # New, more complicated but more useful / configurable behaviour.
        # Uses different keys for inserts / updates / deletes.
        # Currently only used when --delta-filter is requested.
        # "--" means delete's old value
        # "-" means update's old value
        # "+" means update's new value.
        # "++" means insert's new value.

        if delta_filter is None:
            from .key_filters import DeltaFilter

            delta_filter = DeltaFilter.MATCH_ALL
        result = {}
        if self.old and self.new:
            result["-"] = self.old_value if "-" in delta_filter else None
            result["+"] = self.new_value if "+" in delta_filter else None
        elif self.old:
            result["--"] = self.old_value if "--" in delta_filter else None
        elif self.new:
            result["++"] = self.new_value if "++" in delta_filter else None
        return result


class RichDict(UserDict):
    """
    A RichDict is a UserDict with some extra features, mostly useful when dealing with nested dicts with a
    well-defined structure.  It enforces that each node has children of the expected type. Using this
    type information it also supports getting or setting items deep in the nested tree using recursive_get
    or recursive_set, even if this involves creating extra dicts to contain the new value.
    """

    child_type: type | tuple[type, ...] | None = None

    def __init__(self, *args, **kwargs):
        if type(self) == RichDict:
            raise ValueError("RichDict is abstract - use a concrete subclass")
        super().__init__(*args, **kwargs)

    def ensure_child_type(self, key, value):
        # Check that the value is of the correct type
        if not isinstance(value, self.child_type):
            # Check if the child_type is a tuple of types, and if so, print a more helpful error message
            if isinstance(self.child_type, tuple):
                child_type_joint = ", ".join([t.__name__ for t in self.child_type])
                child_type_str = f"one of the types: {child_type_joint}"
            else:
                child_type_str = self.child_type.__name__
            raise TypeError(
                f"{type(self).__name__} accepts children of type {child_type_str} "
                f"but received {type(value).__name__}"
            )

    def __setitem__(self, key, value):
        self.ensure_child_type(key, value)
        super().__setitem__(key, value)

    def copy(self):
        return self.__class__(self)

    def empty_copy(self):
        return self.__class__()

    def __eq__(self, other):
        if type(self) != type(other):
            return False
        return super().__eq__(other)

    def __str__(self):
        # RichDicts are often deeply nested, so just show the keys for brevity.
        name = self.__class__.__name__
        return f"{name}(keys={{{','.join(repr(k) for k in self.keys())}}}))"

    __repr__ = __str__

    def recursive_len(self, max_depth=None):
        if max_depth == 1:
            return len(self)
        total = 0
        max_depth = max_depth - 1 if max_depth else None
        for child in self.values():
            total += child.recursive_len(max_depth)
        return total

    def recursive_get(self, keys, default=None):
        """Given a list of keys ["a", "b", "c"] returns self["a"]["b"]["c"] if it exists, or default."""
        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            return self.get(keys[0], None)
        key, *keys = keys
        child = self.get(key)
        return child.recursive_get(keys) if child is not None else default

    def recursive_set(self, keys, value):
        """
        Given a list of keys ["a", "b", "c"], sets self["a"]["b"]["c"] to value, constructing children as necessary.
        """

        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            self[keys[0]] = value
            return
        key, *keys = keys
        child = self.get(key)
        if child is None:
            child = self.create_empty_child(key)
        child.recursive_set(keys, value)

    def recursive_in(self, keys):
        """Given a list of keys ["a", "b", "c"] returns whether self["a"]["b"]["c"] exists."""
        if len(keys) == 0:
            raise ValueError("No keys")
        elif len(keys) == 1:
            return keys[0] in self
        key, *keys = keys
        child = self.get(key)
        return child.recursive_in(keys) if child is not None else False

    def set_if_nonempty(self, key, value):
        if value:
            self[key] = value

    def create_empty_child(self, key):
        child = self.child_type()
        self[key] = child
        return child

    def prune(self, recurse=True):
        """
        Deletes any empty RichDicts that are children of self.
        If recurse is True, also deletes non-empty RichDicts, as long as they only contain empty RichDicts in the end.
        """
        for key, value in list(self.items()):
            if key == "data_changes" and value == False and len(self) == 1:
                del self[key]
            if not isinstance(value, RichDict):
                continue
            if recurse:
                value.prune()
            if not value:
                del self[key]


class Diff(RichDict):
    """
    A Diff is either a dict with the form {key: Delta}, or a dict with the form {key: Diff} for nested diffs.
    This means a RepoDiff can contain zero or more DatasetDiffs, each of which might contain up to two DeltaDiffs
    (one for meta, one for feature), and these DeltaDiffs finally contain the individual Deltas.
    When two diffs are concatenated, all their children with matching keys are recursively concatenated.
    """

    @classmethod
    def concatenated(cls, *diffs):
        """
        Concatenate a list of diffs, returning a new diff.

        Note: This may consume/modify the diffs that were passed in for performance reasons;
        it's not safe to use them after this method returns.
        """
        result = None
        for diff in diffs:
            if diff is None:
                continue
            elif result is None:
                result = diff
            else:
                result += diff
        return result if result is not None else cls()

    def __invert__(self):
        result = self.empty_copy()
        for key, value in self.items():
            result[key] = ~value
        return result

    def __add__(self, other: "Diff"):
        """Concatenate this Diff to the subsequent Diff, by concatenating all children with matching keys."""

        # FIXME: this algorithm isn't perfect when renames are involved.

        other = other.resolve()
        if type(self) != type(other):
            raise TypeError(f"Diff type mismatch: {type(self)} != {type(other)}")

        result = self.empty_copy()

        for key in self.keys() | other.keys():
            lhs = self.get(key)
            rhs = other.get(key)
            if lhs is not None and rhs is not None:
                both = lhs + rhs
                if both:
                    result[key] = both
                else:
                    result.pop(key, None)
            else:
                result[key] = lhs if lhs is not None else rhs
        return result

    def __iadd__(self, other: "Diff"):
        """
        Concatenate this Diff to the subsequent Diff, by concatenating all children with matching keys.
        Slightly faster than __add__, modifies self in place.
        """

        if type(self) != type(other):
            raise TypeError(f"Diff type mismatch: {type(self)} != {type(other)}")

        for key in other.keys():
            lhs = self.get(key)
            rhs = other[key]
            if lhs is not None:
                both = lhs + rhs
                if both:
                    self[key] = both
                else:
                    self.pop(key, None)
            else:
                self[key] = rhs
        return self

    def to_filter(self):
        return {k: v.to_filter() for k, v in self.items()}

    def type_counts(self):
        return {k: v.type_counts() for k, v in self.items()}

    def __json__(self):
        return {k: v for k, v in self.items()}

    def resolve(self):
        """
        Returns a Diff instance with all LazyDeltaDiffs resolved to DeltaDiffs.
        (may mutate the same Diff instance in-place)
        """
        for k, v in self.items():
            new_v = v.resolve()
            if new_v is not v:
                self[k] = new_v

        return self


class DeltaDiff(Diff):
    """
    A DeltaDiff is the inner-most type of Diff, the one that actually contains Deltas.
    Since Deltas know the keys at which they should be stored, a DeltaDiff makes sure to store Deltas at these keys.
    """

    child_type = Delta

    def __init__(self, initial_contents=()):
        if isinstance(initial_contents, (dict, UserDict)):
            super().__init__(initial_contents)
        else:
            super().__init__((delta.key, delta) for delta in initial_contents)

    def __setitem__(self, key, delta):
        if key != delta.key:
            raise ValueError("Delta must be added at the appropriate key")
        super().__setitem__(key, delta)

    def add_delta(self, delta):
        """Add the given delta at the appropriate key."""
        super().__setitem__(delta.key, delta)

    def __invert__(self):
        result = self.empty_copy()
        for key, delta in self.items():
            result.add_delta(~delta)
        return result

    def to_filter(self):
        result = set()
        for delta in self.values():
            if delta.old is not None:
                result.add(str(delta.old.key))
            if delta.new is not None:
                result.add(str(delta.new.key))
        return result

    def type_counts(self):
        result = {}
        for delta in self.values():
            delta_type = delta.type
            result.setdefault(delta_type, 0)
            result[delta_type] += 1
        # Pluralise type names:
        return {f"{delta_type}s": value for delta_type, value in result.items()}

    @classmethod
    def diff_dicts_as_deltas(cls, old, new, delta_flags=0):
        for k in set(old) | set(new):
            old_value = old.get(k)
            new_value = new.get(k)
            if old_value == new_value:
                continue
            old_key_value = (k, old_value) if old_value is not None else None
            new_key_value = (k, new_value) if new_value is not None else None
            delta = Delta(old_key_value, new_key_value)
            delta.flags = delta_flags
            yield delta

    @classmethod
    def diff_dicts(cls, old, new, delta_flags=0):
        result = DeltaDiff()
        for delta in cls.diff_dicts_as_deltas(old, new, delta_flags=delta_flags):
            result.add_delta(delta)
        return result

    def sorted_items(self):
        from numbers import Number

        inf = float("inf")

        def key(item):
            k, v = item
            if k is None:
                return (-inf, "")
            elif isinstance(k, Number):
                return (k, "")
            elif isinstance(k, str):
                return (inf, k)
            else:
                return (inf, str(k))

        return sorted(self.items(), key=key)

    def recursive_len(self, max_depth=None):
        return len(self)

    def resolve(self):
        # don't recurse; it'll be slow and DeltaDiff will never contain any lazy contents
        return self


class LazyDeltaDiff:
    """
    A LazyDeltaDiff is like a DeltaDiff containing an iterator of Deltas, which is lazily evaluated.
    This is useful because there may be a lot of Deltas, and we don't want to store them in memory.

    The only correct way to consume a LazyDeltaDiff populated by a generator is to call `items()`,
    which will consume the iterator as it yields Deltas.
    Calling that method will invalidate the LazyDeltaDiff, so it cannot be used again (doing so will throw an exception)

    To consume the iterator into memory and turn the LazyDeltaDiff into a DeltaDiff, call `resolve()`
    """

    _wrapped_iter: Iterator[Delta]

    def __init__(self, initial_contents: Iterable[Delta] = ()):
        wrapped_iter = iter(initial_contents)
        try:
            first_item = next(wrapped_iter)
        except StopIteration:
            self._wrapped_iter = iter(())
            self._bool = False
        else:
            self._wrapped_iter = chain((first_item,), wrapped_iter)
            self._bool = True
        self._consumed = False

    def __bool__(self):
        return self._bool

    def __add__(self, other):
        resolved = self.resolve()
        resolved += other
        return resolved

    def _check_not_consumed(self):
        if self._consumed:
            raise RuntimeError("LazyDeltaDiff has already been consumed")

    def items(self) -> Iterator[tuple[str, Delta]]:
        """
        Iterates over the items in the LazyDeltaDiff.

        This method consumes the iterator without storing its contents.
        It's not safe to call this method and then consume the DeltaDiff again.
        """
        self._check_not_consumed()
        self._consumed = True
        for delta in self._wrapped_iter:
            yield (delta.key, delta)

    def resolve(self):
        """
        Converts the LazyDeltaDiff into a DeltaDiff by consuming the wrapped iterator.
        """
        self._check_not_consumed()
        self._consumed = True
        return DeltaDiff(self._wrapped_iter)


class DatasetDiff(Diff):
    """A DatasetDiff contains up to two DeltaDiffs, at keys "meta" or "feature"."""

    child_type = (LazyDeltaDiff, DeltaDiff, bool)

    def __json__(self):
        result = {}
        if "meta" in self:
            result["meta"] = {
                key: value.to_plus_minus_dict() for key, value in self["meta"].items()
            }
        if "feature" in self:
            result["feature"] = (value for key, value in self["feature"].sorted_items())
        return result


class RepoDiff(Diff):
    """A RepoDiff contains zero or more DatasetDiffs (one for each dataset that has changes)."""

    child_type = DatasetDiff
