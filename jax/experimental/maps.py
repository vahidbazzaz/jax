# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import threading
import contextlib
import numpy as np
import itertools as it
from collections import OrderedDict, namedtuple
from typing import (Callable, Iterable, Tuple, Optional, Dict, Any, Set,
                    NamedTuple, Union, Sequence)
from warnings import warn
from functools import wraps, partial, partialmethod

from .. import numpy as jnp
from .. import core
from .. import linear_util as lu
from .._src.api import _check_callable, _check_arg
from ..tree_util import (tree_flatten, tree_unflatten, all_leaves, tree_map,
                         tree_leaves)
from .._src.tree_util import _replace_nones
from ..api_util import (flatten_fun_nokwargs, flatten_axes, _ensure_index_tuple,
                        donation_vector)
from .._src import source_info_util
from ..errors import JAXTypeError
from ..interpreters import partial_eval as pe
from ..interpreters import pxla
from ..interpreters import xla
from ..interpreters import batching
from ..interpreters import ad
from ..lib import xla_bridge as xb
from ..lib import xla_client as xc
from .._src.util import (safe_map, safe_zip, HashableFunction,
                         as_hashable_function, unzip2, distributed_debug_log,
                         tuple_replace)
from .._src.lax.parallel import _axis_index_translation_rule

map, unsafe_map = safe_map, map
zip = safe_zip

xops = xc.ops

EXPERIMENTAL_SPMD_LOWERING = False

class FrozenDict:  # dataclasses might remove some boilerplate here
  def __init__(self, *args, **kwargs):
    self.contents = dict(*args, **kwargs)

  allowed_methods = {'items', 'values', 'keys', 'get'}
  def __getattr__(self, name):
    if name in self.allowed_methods:
      return getattr(self.contents, name)
    raise AttributeError(name)

  def __iter__(self):
    return self.contents.__iter__()

  def __len__(self):
    return self.contents.__len__()

  def __getitem__(self, name):
    return self.contents.__getitem__(name)

  def __eq__(self, other):
    return isinstance(other, FrozenDict) and self.contents == other.contents

  def __hash__(self):
    return hash(tuple(self.contents.items()))

  def __repr__(self):
    return f"FrozenDict({self.contents})"

# Multi-dimensional generalized map

AxisName = core.AxisName
ResourceAxisName = AxisName  # Different name just for documentation purposes
Mesh = pxla.Mesh

# TODO: Support sequential mapping
class ResourceEnv:
  __slots__ = ('physical_mesh',)
  physical_mesh: Mesh

  def __init__(self, physical_mesh: Mesh):
    super().__setattr__('physical_mesh', physical_mesh)

  @property
  def physical_resource_axes(self) -> Set[ResourceAxisName]:
    return set(self.physical_mesh.axis_names)

  @property
  def resource_axes(self) -> Set[ResourceAxisName]:
    return self.physical_resource_axes

  @property
  def shape(self):
    return self.physical_mesh.shape

  @property
  def local_shape(self):
    return self.physical_mesh.local_mesh.shape

  def __setattr__(self, name, value):
    raise RuntimeError("ResourceEnv is immutable!")

  def __delattr__(self):
    raise RuntimeError("ResourceEnv is immutable!")

  def __eq__(self, other):
    return (type(other) is ResourceEnv and
            self.physical_mesh == other.physical_mesh)

  def __hash__(self):
    return hash(self.physical_mesh)

  def __repr__(self):
    return f"ResourceEnv({self.physical_mesh!r})"

thread_resources = threading.local()
thread_resources.env = ResourceEnv(Mesh(np.empty((), dtype=object), ()))

@contextlib.contextmanager
def mesh(devices: np.ndarray, axis_names: Sequence[ResourceAxisName]):
  """Declare the hardware resources available in scope of this manager.

  In particular, all ``axis_names`` become valid resource names inside the
  managed block and can be used e.g. in the ``axis_resources`` argument of
  :py:func:`xmap`.

  Args:
    devices: A NumPy ndarray object containing JAX device objects (as
      obtained e.g. from :py:func:`jax.devices`).
    axis_names: A sequence of resource axis names to be assigned to the
      dimensions of the ``devices`` argument. Its length should match the
      rank of ``devices``.

  Example::

    devices = np.array(jax.devices())[:4].reshape((2, 2))
    with mesh(devices, ('x', 'y')):  # declare a 2D mesh with axes 'x' and 'y'
      distributed_out = xmap(
        jnp.vdot,
        in_axes=({0: 'left', 1: 'right'}),
        out_axes=['left', 'right', ...],
        axis_resources={'left': 'x', 'right': 'y'})(x, x.T)
  """
  old_env = getattr(thread_resources, "env", None)
  thread_resources.env = ResourceEnv(Mesh(devices, axis_names))
  try:
    yield
  finally:
    thread_resources.env = old_env

_next_resource_id = 0
class _UniqueResourceName:
  def __init__(self, uid, tag=None):
    self.uid = uid
    self.tag = tag
  def __eq__(self, other):
    return type(other) is _UniqueResourceName and self.uid == other.uid
  def __hash__(self):
    return hash(self.uid)
  def __repr__(self):
    return f"<UniqueResource {self.tag} {self.uid}>"

def fresh_resource_name(tag=None):
  global _next_resource_id
  try:
    return _UniqueResourceName(_next_resource_id, tag)
  finally:
    _next_resource_id += 1


# This is really a Dict[AxisName, int], but we don't define a
# pytree instance for it, so that it is treated as a leaf.
class AxisNamePos(FrozenDict):
  user_repr: str
  expected_rank: Optional[int] = None

  def __init__(self, *args, user_repr, **kwargs):
    super().__init__(*args, **kwargs)
    self.user_repr = user_repr

class AxisNamePosWithRank(AxisNamePos):
  def __init__(self, *args, expected_rank, **kwargs):
    super().__init__(*args, **kwargs)
    self.expected_rank = expected_rank


# str(...) == 'Ellipsis' which is really annoying
class DotDotDotRepr:
  def __repr__(self): return '...'


def _parse_entry(arg_name, entry):
  # Dictionaries mapping axis names to positional axes
  if isinstance(entry, dict) and all(isinstance(v, int) for v in entry.keys()):
    result = AxisNamePos(((name, axis) for axis, name in entry.items()),
                         user_repr=str(entry))
    num_mapped_dims = len(entry)
  # Non-empty lists or tuples that optionally terminate with an ellipsis
  elif isinstance(entry, (tuple, list)):
    if entry and entry[-1] == ...:
      constr = AxisNamePos
      entry = entry[:-1]
      user_repr = str(entry + [DotDotDotRepr()])
    else:
      constr = partial(AxisNamePosWithRank, expected_rank=len(entry))
      user_repr = str(entry)
    result = constr(((name, axis) for axis, name in enumerate(entry)
                     if name is not None),
                    user_repr=user_repr)
    num_mapped_dims = sum(name is not None for name in entry)
  else:
    raise TypeError(f"""\
Value mapping specification in xmap {arg_name} pytree can be either:
- lists of axis names (possibly ending with the ellipsis object: ...)
- dictionaries that map axis names to positional axes (integers)
but got: {entry}""")
  if len(result) != num_mapped_dims:
    raise ValueError(f"Named axes should be unique within each {arg_name} argument "
                     f"specification, but one them is: {entry}")
  for axis in result.values():
    if axis < 0:
      raise ValueError(f"xmap doesn't support negative axes in {arg_name}")
  return result

def _is_axes_leaf(entry):
  if isinstance(entry, dict) and all_leaves(entry.values()):
    return True
  # NOTE: `None`s are not considered leaves by `all_leaves`
  if isinstance(entry, (tuple, list)) and all_leaves(v for v in entry if v is not None):
    return True
  return False

def _prepare_axes(axes, arg_name):
  entries, treedef = tree_flatten(axes, is_leaf=_is_axes_leaf)
  entries = map(partial(_parse_entry, arg_name), entries)
  return tree_unflatten(treedef, entries), entries, treedef


# TODO: Some syntactic sugar to make the API more usable in a single-axis case?
# TODO: Are the resource axes scoped lexically or dynamically? Dynamically for now!
def xmap(fun: Callable,
         in_axes,
         out_axes,
         *,
         axis_sizes: Dict[AxisName, int] = {},
         axis_resources: Dict[AxisName, Union[ResourceAxisName, Tuple[ResourceAxisName, ...]]] = {},
         donate_argnums: Union[int, Sequence[int]] = (),
         backend: Optional[str] = None):
  """Assign a positional signature to a program that uses named array axes.

  .. warning::
    This is an experimental feature and the details can change at
    any time. Use at your own risk!

  .. warning::
    This docstring is aspirational. Not all features of the named axis
    programming model have been implemented just yet.

  The usual programming model of JAX (or really NumPy) associates each array
  with two pieces of metadata describing its type: the element type (``dtype``)
  and the ``shape``. :py:func:`xmap` extends this model by adding support for
  *named axes*.  In particular, each array used in a function wrapped by
  :py:func:`xmap` can additionally have a non-empty ``named_shape`` attribute,
  which can be used to query the set of named axes (introduced by
  :py:func:`xmap`) appearing in that value along with their shapes.
  Furthermore, in most places where positional axis indices are allowed (for
  example the `axes` arguments in :py:func:`sum`), bound axis names are also
  accepted. The :py:func:`einsum` language is extended inside :py:func:`xmap`
  to additionally allow contractions that involve named axes.  Broadcasting of
  named axes happens *by name*, i.e. all axes with equal names are expected to
  have equal shapes in all arguments of a broadcasting operation, while the
  result has a (set) union of all named axes.  The positional semantics of the
  program remain unchanged, and broadcasting still implicitly right-aligns
  positional axes for unification. For an extended description of the
  :py:func:`xmap` programming model, please refer to the :py:func:`xmap`
  tutorial notebook in main JAX documentation.

  Note that since all top-level JAX expressions are interpreted in the NumPy
  programming model, :py:func:`xmap` can also be seen as an adapter that
  converts a function that uses named axes (including in arguments and returned
  values) into one that takes and returns values that only have positional
  axes.

  The default lowering strategy of :py:func:`xmap` converts all named axes into
  positional axes, working similarly to multiple applications of
  :py:func:`vmap`. However, this behavior can be further customized by the
  ``axis_resources`` argument.  When specified, each axis introduced by
  :py:func:`xmap` can be assigned to one or more *resource axes*. Those include
  the axes of the hardware mesh, as defined by the :py:func:`mesh` context
  manager. Each value that has a named axis in its ``named_shape`` will be
  partitioned over all mesh axes that axis is assigned to. Hence,
  :py:func:`xmap` can be seen as an alternative to :py:func:`pmap` that also
  exposes a way to automatically partition the computation over multiple
  devices.

  .. warning::
    While it is possible to assign multiple axis names to a single resource axis,
    care has to be taken to ensure that none of those named axes co-occur in a
    ``named_shape`` of any value in the named program. At the moment this is
    **completely unchecked** and will result in **undefined behavior**. Final
    release of :py:func:`xmap` will enforce this invariant, but it is work
    in progress.

    Note that you do not have to worry about any of this for as long as no
    resource axis is repeated in ``axis_resources.values()``.

  Note that any assignment of ``axis_resources`` doesn't ever change the
  results of the computation, but only how it is carried out (e.g. how many
  devices are used).  This makes it easy to try out various ways of
  partitioning a single program in many distributed scenarions (both small- and
  large-scale), to maximize the performance.  As such, :py:func:`xmap` can be
  seen as a way to seamlessly interpolate between :py:func:`vmap` and
  :py:func:`pmap`-style execution.

  Args:
    fun: Function that uses named axes. Its arguments and return
      value should be arrays, scalars, or (nested) standard Python containers
      (tuple/list/dict) thereof (in general: valid pytrees).
    in_axes: A Python object with the same container (pytree) structure as the
      signature of arguments to ``fun``, but with a positional-to-named axis
      mapping in place of every array argument. The valid positional-to-named
      mappings are: (1) a ``Dict[int, AxisName]`` specifying that a positional
      dimensions given by dictionary keys are to be converted to named axes
      of given names (2) a list of axis names that ends with the Ellipsis object
      (``...``) in which case a number of leading positional axes of the argument
      will be converted into named axes inside the function. Note that ``in_axes``
      can also be a prefix of the argument container structure, in which case the
      mapping is repeated for all arrays in the collapsed subtree.
    out_axes: A Python object with the same container (pytree) structure as the
      returns of ``fun``, but with a positional-to-named axis mapping in place
      of every returned array. The valid positional-to-named mappings are the same
      as in ``in_axes``. Note that ``out_axes`` can also be a prefix of the return
      container structure, in which case the mapping is repeated for all arrays
      in the collapsed subtree.
    axis_sizes: A dict mapping axis names to their sizes. All axes defined by xmap
      have to appear either in ``in_axes`` or ``axis_sizes``. Sizes of axes
      that appear in ``in_axes`` are inferred from arguments whenever possible.
      In multi-host scenarios, the user-specified sizes are expected to be the
      global axis sizes (and might not match the expected size of local inputs).
    axis_resources: A dictionary mapping the axes introduced in this
      :py:func:`xmap` to one or more resource axes. Any array that has in its
      shape an axis with some resources assigned will be partitioned over the
      resources associated with the respective resource axes.
    backend: This is an experimental feature and the API is likely to change.
      Optional, a string representing the XLA backend. 'cpu', 'gpu', or 'tpu'.

  Returns:
    A version of ``fun`` that takes in arrays with positional axes in place of
    named axes bound in this :py:func:`xmap` call, and results with all named
    axes converted to positional axes. If ``axis_resources`` is specified,
    ``fun`` can additionally execute in parallel on multiple devices.

  For example, :py:func:`xmap` makes it very easy to convert a function that
  computes the vector inner product (such as :py:func:`jax.numpy.vdot`) into
  one that computes a matrix multiplication:

  >>> import jax.numpy as jnp
  >>> x = jnp.arange(10).reshape((2, 5))
  >>> xmap(jnp.vdot,
  ...      in_axes=({0: 'left'}, {1: 'right'}),
  ...      out_axes=['left', 'right', ...])(x, x.T)
  DeviceArray([[ 30,  80],
               [ 80, 255]], dtype=int32)

  Note that the contraction in the program is performed over the positional axes,
  while named axes are just a convenient way to achieve batching. While this
  might seem like a silly example at first, it might turn out to be useful in
  practice, since with conjuction with ``axis_resources`` this makes it possible
  to implement a distributed matrix-multiplication in just a few lines of code::

    devices = np.array(jax.devices())[:4].reshape((2, 2))
    with mesh(devices, ('x', 'y')):  # declare a 2D mesh with axes 'x' and 'y'
      distributed_out = xmap(
        jnp.vdot,
        in_axes=({0: 'left'}, {1: 'right'}),
        out_axes=['left', 'right', ...],
        axis_resources={'left': 'x', 'right': 'y'})(x, x.T)

  Still, the above examples are quite simple. After all, the xmapped
  computation was a simple NumPy function that didn't use the axis names at all!
  So, let's explore a slightly larger example which is linear regression::

    def regression_loss(x, y, w, b):
      # Contract over in_features. Batch and out_features are present in
      # both inputs and output, so they don't need to be mentioned
      y_pred = jnp.einsum('{in_features},{in_features}->{}', x, w) + b
      error = jnp.sum((y - y_pred) ** 2, axis='out_features')
      return jnp.mean(error, axis='batch')

    xmap(regression_loss,
         in_axes=(['batch', 'in_features', ...],
                  ['batch', 'out_features', ...],
                  ['in_features', 'out_features', ...],
                  ['out_features', ...]),
         out_axes={})  # Loss is reduced over all axes, including batch!

  .. note::
    When using ``axis_resources`` along with a mesh that is controled by
    multiple JAX hosts, keep in mind that in any given process :py:func:`xmap`
    only expects the data slice that corresponds to its local devices to be
    specified. This is in line with the current multi-host :py:func:`pmap`
    programming model.
  """
  warn("xmap is an experimental feature and probably has bugs!")
  _check_callable(fun)

  # To be a tree prefix of the positional args tuple, in_axes can never be a
  # list: if in_axes is not a leaf, it must be a tuple of trees. However,
  # in cases like these users expect tuples and lists to be treated
  # essentially interchangeably, so we canonicalize lists to tuples here
  # rather than raising an error. https://github.com/google/jax/issues/2367
  if isinstance(in_axes, list) and not _is_axes_leaf(in_axes):
    in_axes = tuple(in_axes)
  if isinstance(out_axes, list) and not _is_axes_leaf(out_axes):
    out_axes = tuple(out_axes)

  if in_axes == ():  # Allow empty argument lists
    in_axes, in_axes_entries = (), []
  else:
    in_axes, in_axes_entries, _ = _prepare_axes(in_axes, "in_axes")
  if out_axes == ():
    raise ValueError("xmapped functions cannot have no return values")
  else:
    out_axes, out_axes_entries, out_axes_treedef = _prepare_axes(out_axes, "out_axes")
    out_axes_entries = tuple(out_axes_entries)  # Make entries hashable

  axis_sizes_names = set(axis_sizes.keys())
  in_axes_names = set(it.chain(*(spec.keys() for spec in in_axes_entries)))
  defined_names = axis_sizes_names | in_axes_names
  out_axes_names = set(it.chain(*(spec.keys() for spec in out_axes_entries)))
  normalized_axis_resources: Dict[AxisName, Tuple[ResourceAxisName, ...]] = \
      {axis: (resources if isinstance(resources, tuple) else (resources,))
       for axis, resources in axis_resources.items()}
  for axis in defined_names:
    normalized_axis_resources.setdefault(axis, ())
  frozen_axis_resources = FrozenDict(normalized_axis_resources)
  necessary_resources = set(it.chain(*frozen_axis_resources.values()))

  axes_with_resources = set(frozen_axis_resources.keys())
  if axes_with_resources > defined_names:
    raise ValueError(f"All axes that were assigned resources have to appear in "
                     f"in_axes or axis_sizes, but the following are missing: "
                     f"{axes_with_resources - defined_names}")
  if out_axes_names > defined_names:
    raise ValueError(f"All axis names appearing in out_axes must also appear in "
                     f"in_axes or axis_sizes, but the following are missing: "
                     f"{out_axes_names - defined_names}")

  for axis, resources in frozen_axis_resources.items():
    if len(set(resources)) != len(resources):
      raise ValueError(f"Resource assignment of a single axis must be a tuple of "
                       f"distinct resources, but specified {resources} for axis {axis}")

  donate_argnums = _ensure_index_tuple(donate_argnums)

  # A little performance optimization to avoid iterating over all args unnecessarily
  has_input_rank_assertions = any(spec.expected_rank is not None for spec in in_axes_entries)
  has_output_rank_assertions = any(spec.expected_rank is not None for spec in out_axes_entries)

  @wraps(fun)
  def fun_mapped(*args):
    # Putting this outside of fun_mapped would make resources lexically scoped
    resource_env = thread_resources.env
    available_resources = set(resource_env.shape.keys())

    if necessary_resources > available_resources:
      raise ValueError(f"In-scope resources are insufficient to execute the "
                       f"xmapped function. The missing resources are: "
                       f"{necessary_resources - available_resources}")

    args_flat, in_tree = tree_flatten(args)
    for arg in args_flat: _check_arg(arg)
    fun_flat, out_tree = flatten_fun_nokwargs(lu.wrap_init(fun), in_tree)
    if donate_argnums:
      donated_invars = donation_vector(donate_argnums, args, ())
    else:
      donated_invars = (False,) * len(args_flat)
    in_axes_flat = flatten_axes("xmap in_axes", in_tree, in_axes)

    # We flatten out_axes into leaves and treedef, because some pytree containers
    # might be unhashable.
    out_axes_thunk = HashableFunction(
      lambda: tuple(flatten_axes("xmap out_axes", out_tree(), out_axes)),
      closure=(out_axes_entries, out_axes_treedef))

    axis_resource_count = _get_axis_resource_count(normalized_axis_resources, resource_env)
    for axis, size in axis_sizes.items():
      resources = axis_resource_count[axis]
      if size % resources.nglobal != 0:
        global_size = "Global size" if resources.distributed else "Size"
        raise ValueError(f"{global_size} of axis {axis} ({size}) is not divisible "
                         f"by the total number of resources assigned to this axis "
                         f"({normalized_axis_resources[axis]}, {resources.nglobal} in total)")
    frozen_global_axis_sizes = _get_axis_sizes(args_flat, in_axes_flat,
                                               axis_sizes, axis_resource_count)

    missing_sizes = defined_names - set(frozen_global_axis_sizes.keys())
    if missing_sizes:
      raise ValueError(f"Failed to infer size of axes: {', '.join(unsafe_map(str, missing_sizes))}. "
                       f"You've probably passed in empty containers in place of arguments that had "
                       f"those axes in their in_axes. Provide the sizes of missing axes explicitly "
                       f"via axis_sizes to fix this error.")

    if has_input_rank_assertions:
      for arg, spec in zip(args_flat, in_axes_flat):
        if spec.expected_rank is not None and spec.expected_rank != arg.ndim:
          raise ValueError(f"xmap argument has an in_axes specification of {spec.user_repr}, "
                           f"which asserts that it should be of rank {spec.expected_rank}, "
                           f"but the argument has rank {arg.ndim} (and shape {arg.shape})")
    out_flat = xmap_p.bind(
      fun_flat, *args_flat,
      name=getattr(fun, '__name__', '<unnamed function>'),
      in_axes=tuple(in_axes_flat),
      out_axes_thunk=out_axes_thunk,
      donated_invars=donated_invars,
      global_axis_sizes=frozen_global_axis_sizes,
      axis_resources=frozen_axis_resources,
      resource_env=resource_env,
      backend=backend,
      spmd_in_axes=None,
      spmd_out_axes_thunk=None)
    if has_output_rank_assertions:
      for out, spec in zip(out_flat, out_axes_thunk()):
        if spec.expected_rank is not None and spec.expected_rank != out.ndim:
          raise ValueError(f"xmap output has an out_axes specification of {spec.user_repr}, "
                           f"which asserts that it should be of rank {spec.expected_rank}, "
                           f"but the output has rank {out.ndim} (and shape {out.shape})")
    return tree_unflatten(out_tree(), out_flat)

  return fun_mapped

def xmap_impl(fun: lu.WrappedFun, *args, name, in_axes, out_axes_thunk, donated_invars,
              global_axis_sizes, axis_resources, resource_env, backend,
              spmd_in_axes, spmd_out_axes_thunk):
  in_avals = [core.raise_to_shaped(core.get_aval(arg)) for arg in args]
  xmap_callable = make_xmap_callable(
      fun, name, in_axes, out_axes_thunk, donated_invars, global_axis_sizes,
      axis_resources, resource_env, backend,
      spmd_in_axes, spmd_out_axes_thunk,
      *in_avals)
  distributed_debug_log(("Running xmapped function", name),
                        ("python function", fun.f),
                        ("mesh", resource_env.physical_mesh),
                        ("abstract args", in_avals))
  return xmap_callable(*args)

@lu.cache
def make_xmap_callable(fun: lu.WrappedFun,
                       name,
                       in_axes, out_axes_thunk, donated_invars,
                       global_axis_sizes, axis_resources, resource_env, backend,
                       spmd_in_axes, spmd_out_axes_thunk,
                       *in_avals):
  plan = EvaluationPlan.from_axis_resources(axis_resources, resource_env, global_axis_sizes)

  # TODO: Making axis substitution final style would allow us to avoid
  #       tracing to jaxpr here
  mapped_in_avals = [_delete_aval_axes(aval, in_axes)
                     for aval, in_axes in zip(in_avals, in_axes)]
  with core.extend_axis_env_nd(global_axis_sizes.items()):
    jaxpr, out_avals, consts = pe.trace_to_jaxpr_final(fun, mapped_in_avals)
  out_axes = out_axes_thunk()
  _check_out_avals_vs_out_axes(out_avals, out_axes, global_axis_sizes)
  _resource_typing_xmap(dict(axis_resources=axis_resources,
                             out_axes=out_axes,
                             call_jaxpr=jaxpr,
                             name=name),
                        None, {})
  jaxpr = plan.subst_axes_with_resources(jaxpr)

  f = lu.wrap_init(core.jaxpr_as_fun(core.ClosedJaxpr(jaxpr, consts)))
  f = hide_mapped_axes(f, tuple(in_axes), tuple(out_axes))
  f = plan.vectorize(f, in_axes, out_axes)

  used_resources = _jaxpr_resources(jaxpr, resource_env) | set(it.chain(*axis_resources.values()))
  used_mesh_axes = used_resources & resource_env.physical_resource_axes
  if used_mesh_axes:
    assert spmd_in_axes is None and spmd_out_axes_thunk is None  # No outer xmaps, so should be None
    submesh = resource_env.physical_mesh[sorted(used_mesh_axes, key=str)]
    mesh_in_axes, mesh_out_axes = plan.to_mesh_axes(in_axes, out_axes)
    return pxla.mesh_callable(f,
                              name,
                              backend,
                              submesh,
                              mesh_in_axes,
                              mesh_out_axes,
                              donated_invars,
                              EXPERIMENTAL_SPMD_LOWERING,
                              *in_avals,
                              tile_by_mesh_axes=True)
  else:
    return xla._xla_callable(f, None, backend, name, donated_invars,
                             *((a, None) for a in in_avals))

class EvaluationPlan(NamedTuple):
  """Encapsulates preprocessing common to top-level xmap invocations and its translation rule."""
  resource_env: ResourceEnv
  physical_axis_resources: Dict[AxisName, Tuple[ResourceAxisName, ...]]
  axis_subst_dict: Dict[AxisName, Tuple[ResourceAxisName, ...]]
  axis_vmap_size: Dict[AxisName, Optional[int]]

  @property
  def axis_subst(self) -> core.AxisSubst:
    return lambda name: self.axis_subst_dict.get(name, (name,))

  @property
  def resource_axis_env(self):
    env = dict(self.resource_env.shape)
    for axis, size in self.axis_vmap_size.items():
      if size is None:
        continue
      vmap_axis = self.axis_subst_dict[axis][-1]
      env[vmap_axis] = size
    return env

  @classmethod
  def from_axis_resources(cls,
                          axis_resources: Dict[AxisName, Tuple[ResourceAxisName, ...]],
                          resource_env: ResourceEnv,
                          global_axis_sizes: Dict[AxisName, int]):
    # TODO: Support sequential resources
    physical_axis_resources = axis_resources  # NB: We only support physical resources at the moment
    axis_resource_count = _get_axis_resource_count(axis_resources, resource_env)
    axis_subst_dict = dict(axis_resources)
    axis_vmap_size: Dict[AxisName, Optional[int]] = {}
    for naxis, raxes in axis_resources.items():
      num_resources = axis_resource_count[naxis]
      assert global_axis_sizes[naxis] % num_resources.nglobal == 0
      local_tile_size = global_axis_sizes[naxis] // num_resources.nglobal
      # We have to vmap when there are no resources (to handle the axis name!) or
      # when every resource gets chunks of values.
      if not raxes or local_tile_size > 1:
        axis_vmap_size[naxis] = local_tile_size
        axis_subst_dict[naxis] += (fresh_resource_name(naxis),)
      else:
        axis_vmap_size[naxis] = None
    return cls(resource_env, physical_axis_resources, axis_subst_dict, axis_vmap_size)

  def subst_axes_with_resources(self, jaxpr):
    try:
      with core.extend_axis_env_nd(self.resource_axis_env.items()):
        return core.subst_axis_names_jaxpr(jaxpr, self.axis_subst)
    except core.DuplicateAxisNameError:
      raise AssertionError("Incomplete resource type-checking? Please open a bug report!")

  def vectorize(self, f: lu.WrappedFun, in_axes, out_axes):
    for naxis, raxes in self.axis_subst_dict.items():
      local_tile_size = self.axis_vmap_size[naxis]
      if local_tile_size is None:
        continue
      vaxis = raxes[-1]
      map_in_axes = tuple(unsafe_map(lambda spec: spec.get(naxis, None), in_axes))
      map_out_axes = tuple(unsafe_map(lambda spec: spec.get(naxis, None), out_axes))
      f = batching.vtile(f, map_in_axes, map_out_axes, tile_size=local_tile_size, axis_name=vaxis)
    return f

  def to_mesh_axes(self, in_axes, out_axes):
    """
    Convert in/out_axes parameters ranging over logical dimensions to
    in/out_axes that range over the mesh dimensions.
    """
    def to_mesh(axes):
      return OrderedDict((physical_axis, pos_axis)
                         for logical_axis, pos_axis in axes.items()
                         for physical_axis in self.physical_axis_resources[logical_axis])
    return (tuple(unsafe_map(to_mesh, in_axes)),
            tuple(unsafe_map(to_mesh, out_axes)))

def _check_out_avals_vs_out_axes(out_avals: Sequence[core.AbstractValue],
                                 out_axes: Sequence[AxisNamePos],
                                 global_axis_sizes: Dict[AxisName, int]):
  defined_axes = set(global_axis_sizes)
  for aval, axes in zip(out_avals, out_axes):
    if not isinstance(aval, core.ShapedArray):
      if axes:
        raise AssertionError(f"Only array abstract values can have non-empty "
                             f"out_axes, but {aval} has {axes}")
      continue
    undeclared_axes = (set(aval.named_shape) - set(axes)) & defined_axes
    if undeclared_axes:
      undeclared_axes_str = sorted([str(axis) for axis in undeclared_axes])
      raise TypeError(f"One of xmap results has an out_axes specification of "
                      f"{axes.user_repr}, but is actually mapped along more axes "
                      f"defined by this xmap call: {', '.join(undeclared_axes_str)}")

# -------- xmap primitive and its transforms --------

# xmap has a different set of parameters than pmap, so we make it its own primitive type
class XMapPrimitive(core.MapPrimitive):  # Not really a map, but it gives us a few good defaults
  def __init__(self):
    super().__init__('xmap')
    self.def_impl(xmap_impl)
    self.def_custom_bind(self.bind)

  def bind(self, fun, *args, **params):
    assert len(params['in_axes']) == len(args)
    return core.call_bind(self, fun, *args, **params)  # type: ignore

  def process(self, trace, fun, tracers, params):
    return trace.process_xmap(self, fun, tracers, params)

  def post_process(self, trace, out_tracers, params):
    raise NotImplementedError

xmap_p = XMapPrimitive()
core.EvalTrace.process_xmap = core.EvalTrace.process_call  # type: ignore
def _process_xmap_default(self, call_primitive, f, tracers, params):
  raise NotImplementedError(f"{type(self)} must override process_xmap to handle xmap")
core.Trace.process_xmap = _process_xmap_default  # type: ignore

def _xmap_axis_subst(params, subst):
  def shadowed_subst(name):
    return (name,) if name in params['global_axis_sizes'] else subst(name)
  with core.extend_axis_env_nd(params['global_axis_sizes'].items()):
    new_jaxpr = core.subst_axis_names_jaxpr(params['call_jaxpr'], shadowed_subst)
  return dict(params, call_jaxpr=new_jaxpr)
core.axis_substitution_rules[xmap_p] = _xmap_axis_subst

# NOTE: We don't have to handle spmd_{in|out}_axes here, because
# SPMD batching always gets involved as the last transform before XLA translation
ad.JVPTrace.process_xmap = ad.JVPTrace.process_call  # type: ignore
ad.call_param_updaters[xmap_p] = ad.call_param_updaters[xla.xla_call_p]

def _typecheck_xmap(
    *in_avals, call_jaxpr, name, in_axes, out_axes, donated_invars,
    global_axis_sizes, axis_resources, resource_env, backend,
    spmd_in_axes, spmd_out_axes):
  binder_in_avals = [_insert_aval_axes(v.aval, a_in_axes, global_axis_sizes)
                     for v, a_in_axes in zip(call_jaxpr.invars, in_axes)]
  for binder_in_aval, in_aval in zip(binder_in_avals, in_avals):
    core.typecheck_assert(
        core.typecompat(binder_in_aval, in_aval),
        f"xmap passes operand {in_aval} to jaxpr expecting {binder_in_aval}")

  mapped_in_avals = [_delete_aval_axes(a, a_in_axes)
                     for a, a_in_axes in zip(in_avals, in_axes)]
  with core.extend_axis_env_nd(global_axis_sizes.items()):
    core._check_jaxpr(call_jaxpr, mapped_in_avals)

  mapped_out_avals = [v.aval for v in call_jaxpr.outvars]
  out_avals = [_insert_aval_axes(a, a_out_axes, global_axis_sizes)
               for a, a_out_axes in zip(mapped_out_avals, out_axes)]
  return out_avals
core.custom_typechecks[xmap_p] = _typecheck_xmap

def show_axes(axes):
  return ", ".join(sorted([f"`{a}`" for a in axes]))

def _resource_typing_xmap(params,
                          source_info: Optional[source_info_util.Traceback],
                          outer_axis_resources):
  axis_resources = params['axis_resources']
  inner_axis_resources = dict(outer_axis_resources)
  inner_axis_resources.update(axis_resources)
  if len(inner_axis_resources) < len(outer_axis_resources) + len(axis_resources):
    overlap = set(outer_axis_resources) & set(axis_resources)
    raise JAXTypeError(
        f"Detected disallowed xmap axis name shadowing at "
        f"{source_info_util.summarize(source_info)} "
        f"(shadowed axes: {show_axes(overlap)})")

  call_jaxpr = params['call_jaxpr']
  pxla.resource_typecheck(
      params['call_jaxpr'], inner_axis_resources,
      lambda: (f"an xmapped function {params['name']} " +
               (f"(xmap called at {source_info_util.summarize(source_info)})"
                if source_info else "")))

  for v, axes in zip(call_jaxpr.outvars, params['out_axes']):
    broadcast_axes = set(axes) - set(v.aval.named_shape)
    used_resources = set(it.chain.from_iterable(
        inner_axis_resources[a] for a in v.aval.named_shape))
    for baxis in broadcast_axes:
      baxis_resources = set(inner_axis_resources[baxis])
      overlap = baxis_resources & used_resources
      if overlap:
        resource_to_axis = {}
        for axis in v.aval.named_shape:
          for raxis in inner_axis_resources[axis]:
            resource_to_axis[raxis] = axis
        partitioning_axes = set(resource_to_axis[raxis] for raxis in overlap)
        raise JAXTypeError(
            f"One of xmapped function ({params['name']}) outputs is broadcast "
            f"along axis `{baxis}` which is assigned to resources "
            f"{show_axes(baxis_resources)}, but the output is already "
            f"partitioned along {show_axes(overlap)}, because its "
            f"named shape contains {show_axes(partitioning_axes)}")
pxla.custom_resource_typing_rules[xmap_p] = _resource_typing_xmap


# This is DynamicJaxprTrace.process_map with some very minor modifications
def _dynamic_jaxpr_process_xmap(self, primitive, f, tracers, params):
  from jax.interpreters.partial_eval import (
    trace_to_subjaxpr_dynamic, DynamicJaxprTracer, source_info_util,
    convert_constvars_jaxpr, new_jaxpr_eqn)
  assert primitive is xmap_p
  in_avals = [t.aval for t in tracers]
  global_axis_sizes = params['global_axis_sizes']
  mapped_in_avals = [_delete_aval_axes(a, a_in_axes)
                     for a, a_in_axes in zip(in_avals, params['in_axes'])]
  with core.extend_axis_env_nd(global_axis_sizes.items()):
    jaxpr, mapped_out_avals, consts = trace_to_subjaxpr_dynamic(
        f, self.main, mapped_in_avals)
  out_axes = params['out_axes_thunk']()
  if params['spmd_out_axes_thunk'] is not None:
    spmd_out_axes = params['spmd_out_axes_thunk']()
  else:
    spmd_out_axes = None
  axis_resource_count = _get_axis_resource_count(params['axis_resources'],
                                                 params['resource_env'])
  local_axis_sizes = {axis: axis_resource_count[axis].to_local(global_size)
                      for axis, global_size in global_axis_sizes.items()}
  out_avals = [_insert_aval_axes(a, a_out_axes, local_axis_sizes)
               for a, a_out_axes in zip(mapped_out_avals, out_axes)]
  _check_out_avals_vs_out_axes(out_avals, out_axes, params['global_axis_sizes'])
  source_info = source_info_util.current()
  out_tracers = [DynamicJaxprTracer(self, a, source_info) for a in out_avals]
  invars = map(self.getvar, tracers)
  constvars = map(self.getvar, map(self.instantiate_const, consts))
  outvars = map(self.makevar, out_tracers)
  new_in_axes = (AxisNamePos(user_repr='{}'),) * len(consts) + params['in_axes']
  if params['spmd_in_axes'] is None:
    new_spmd_in_axes = None
  else:
    new_spmd_in_axes = (None,) * len(consts) + params['spmd_in_axes']
  new_donated_invars = (False,) * len(consts) + params['donated_invars']
  with core.extend_axis_env_nd(global_axis_sizes.items()):
    call_jaxpr = convert_constvars_jaxpr(jaxpr)
  new_params = dict(params, in_axes=new_in_axes, out_axes=out_axes,
                    donated_invars=new_donated_invars,
                    spmd_in_axes=new_spmd_in_axes,
                    spmd_out_axes=spmd_out_axes,
                    call_jaxpr=call_jaxpr)
  del new_params['out_axes_thunk']
  del new_params['spmd_out_axes_thunk']
  eqn = new_jaxpr_eqn([*constvars, *invars], outvars, primitive,
                      new_params, source_info)
  self.frame.eqns.append(eqn)
  return out_tracers
pe.DynamicJaxprTrace.process_xmap = _dynamic_jaxpr_process_xmap  # type: ignore


def _batch_trace_update_spmd_axes(
    spmd_in_axes, spmd_out_axes_thunk,
    axis_name, dims, dims_out_thunk):
  """Extends spmd in and out axes with the position of the trace's batch dimension."""
  not_mapped = batching.not_mapped
  def insert_spmd_axis(axes, nd):
    if axes is None:
      axes = ()
    too_short = (nd + 1) - len(axes)
    if too_short > 0:
      axes += ((),) * too_short
    return tuple_replace(axes, nd, (axis_name,) + axes[nd])

  if spmd_in_axes is None:
    spmd_in_axes = (None,) * len(dims)
  new_spmd_in_axes = tuple(
    spmd_axes if d is not_mapped else insert_spmd_axis(spmd_axes, d)
    for spmd_axes, d in zip(spmd_in_axes, dims))

  @as_hashable_function(closure=spmd_out_axes_thunk)
  def new_spmd_out_axes_thunk():
    dims_out = dims_out_thunk()
    if spmd_out_axes_thunk is None:
      spmd_out_axes = (None,) * len(dims_out)
    else:
      spmd_out_axes = spmd_out_axes_thunk()
    return tuple(
      spmd_out_axes if nd is not_mapped else insert_spmd_axis(spmd_out_axes, nd)
      for spmd_out_axes, nd in zip(spmd_out_axes, dims_out))

  return new_spmd_in_axes, new_spmd_out_axes_thunk

def _batch_trace_process_xmap(self, is_spmd, primitive, f: lu.WrappedFun, tracers, params):
  not_mapped = batching.not_mapped
  vals, dims = unzip2((t.val, t.batch_dim) for t in tracers)
  assert primitive is xmap_p
  if not is_spmd and all(dim is not_mapped for dim in dims):
    return primitive.bind(f, *vals, **params)
  else:
    assert len({x.shape[d] for x, d in zip(vals, dims) if d is not not_mapped}) == 1
    def fmap_dims(axes, f):
      return AxisNamePos(((name, f(axis)) for name, axis in axes.items()),
                         user_repr=axes.user_repr)
    new_in_axes = tuple(
      fmap_dims(in_axes, lambda a: a + (d is not not_mapped and d <= a))
      for d, in_axes in zip(dims, params['in_axes']))
    mapped_dims_in = tuple(
      d if d is not_mapped else d - sum(a < d for a in in_axis.values())
      for d, in_axis in zip(dims, params['in_axes']))
    f, mapped_dims_out = batching.batch_subtrace(f, self.main, mapped_dims_in)
    out_axes_thunk: Callable[[], Sequence[AxisNamePos]] = params['out_axes_thunk']
    dims_out_thunk = lambda: tuple(d if d is not_mapped else axis_after_insertion(d, out_axes)
                                   for d, out_axes in zip(mapped_dims_out(), out_axes_thunk()))
    def axis_after_insertion(axis, inserted_named_axes):
      for inserted_axis in sorted(inserted_named_axes.values()):
        if inserted_axis >= axis:
          break
        axis += 1
      return axis
    # NOTE: This assumes that the choice of the dimensions over which outputs
    #       are batched is entirely dependent on the function and not e.g. on the
    #       data or its shapes.
    @as_hashable_function(closure=out_axes_thunk)
    def new_out_axes_thunk():
      return tuple(
        out_axes if d is not_mapped else
        fmap_dims(out_axes, lambda a, nd=axis_after_insertion(d, out_axes): a + (nd <= a))
        for out_axes, d in zip(out_axes_thunk(), mapped_dims_out()))

    if not is_spmd:
      assert params['spmd_in_axes'] is None and params['spmd_out_axes_thunk'] is None
      new_spmd_in_axes = None
      new_spmd_out_axes_thunk = None
    else:
      new_spmd_in_axes, new_spmd_out_axes_thunk = _batch_trace_update_spmd_axes(
        params['spmd_in_axes'], params['spmd_out_axes_thunk'],
        self.axis_name, dims, dims_out_thunk)

    new_params = dict(params,
                      in_axes=new_in_axes, out_axes_thunk=new_out_axes_thunk,
                      spmd_in_axes=new_spmd_in_axes,
                      spmd_out_axes_thunk=new_spmd_out_axes_thunk)
    vals_out = primitive.bind(f, *vals, **new_params)
    dims_out = dims_out_thunk()
    return [batching.BatchTracer(self, v, d) for v, d in zip(vals_out, dims_out)]
batching.BatchTrace.process_xmap = partialmethod(_batch_trace_process_xmap, False)  # type: ignore
batching.SPMDBatchTrace.process_xmap = partialmethod(_batch_trace_process_xmap, True)  # type: ignore


def _xmap_initial_to_final_params(params):
  out_axes_thunk = HashableFunction(lambda: params['out_axes'],
                                    closure=params['out_axes'])
  if params['spmd_out_axes'] is not None:
    spmd_out_axes_thunk = HashableFunction(lambda: params['spmd_out_axes'],
                                           closure=params['spmd_out_axes'])
  else:
    spmd_out_axes_thunk = None
  bind_params = dict(params,
                     out_axes_thunk=out_axes_thunk,
                     spmd_out_axes_thunk=spmd_out_axes_thunk)
  del bind_params['out_axes']
  del bind_params['spmd_out_axes']
  return bind_params
core.initial_to_final_param_rules[xmap_p] = _xmap_initial_to_final_params

# -------- nested xmap handling --------

def _xmap_translation_rule(*args, **kwargs):
  if EXPERIMENTAL_SPMD_LOWERING:
    return _xmap_translation_rule_spmd(*args, **kwargs)
  else:
    return _xmap_translation_rule_replica(*args, **kwargs)
xla.call_translations[xmap_p] = _xmap_translation_rule

def _xmap_translation_rule_replica(c, axis_env,
                                   in_nodes, name_stack, *,
                                   call_jaxpr, name,
                                   in_axes, out_axes, donated_invars,
                                   global_axis_sizes,
                                   spmd_in_axes, spmd_out_axes,
                                   axis_resources, resource_env, backend):
  assert spmd_in_axes is None and spmd_out_axes is None
  plan = EvaluationPlan.from_axis_resources(axis_resources, resource_env, global_axis_sizes)

  axis_resource_count = _get_axis_resource_count(axis_resources, resource_env)
  if any(resource_count.distributed for resource_count in axis_resource_count.values()):
    raise NotImplementedError
  local_axis_sizes = {axis: axis_resource_count[axis].to_local(global_size)
                      for axis, global_size in global_axis_sizes.items()}

  local_mesh = resource_env.physical_mesh.local_mesh
  local_mesh_shape = local_mesh.shape
  mesh_in_axes, mesh_out_axes = plan.to_mesh_axes(in_axes, out_axes)

  local_avals = [pxla.tile_aval_nd(
                    local_mesh_shape, aval_mesh_in_axes,
                    _insert_aval_axes(v.aval, aval_in_axes, local_axis_sizes))
                 for v, aval_in_axes, aval_mesh_in_axes
                 in zip(call_jaxpr.invars, in_axes, mesh_in_axes)]
  # We have to substitute before tracing, because we want the vectorized
  # axes to be used in the jaxpr.
  resource_call_jaxpr = plan.subst_axes_with_resources(call_jaxpr)
  f = lu.wrap_init(core.jaxpr_as_fun(core.ClosedJaxpr(resource_call_jaxpr, ())))
  f = hide_mapped_axes(f, tuple(in_axes), tuple(out_axes))
  f = plan.vectorize(f, in_axes, out_axes)
  # NOTE: We don't extend the resource env with the mesh shape, because those
  #       resources are already in scope! It's the outermost xmap that introduces
  #       them!
  vectorized_jaxpr, out_avals, consts = pe.trace_to_jaxpr_final(f, local_avals)
  _check_out_avals_vs_out_axes(out_avals, out_axes, global_axis_sizes)
  assert not consts

  tiled_ins = (
    _xla_tile(c, axis_env, in_node, arg_in_axes, local_mesh_shape)
    if v.aval is not core.abstract_unit else in_node
    for v, in_node, arg_in_axes in zip(call_jaxpr.invars, in_nodes, mesh_in_axes))

  # NOTE: We don't extend the resource env with the mesh shape, because those
  #       resources are already in scope! It's the outermost xmap that introduces
  #       them!
  # We in-line here rather than generating a Call HLO as in the xla_call
  # translation rule just because the extra tuple stuff is a pain.
  tiled_outs = xla.jaxpr_subcomp(
      c, vectorized_jaxpr, backend, axis_env, (),
      xla.extend_name_stack(name_stack, xla.wrap_name(name, 'xmap')), *tiled_ins)

  outs = [_xla_untile(c, axis_env, tiled_out, ans_out_axes, local_mesh_shape, backend)
          if v.aval is not core.abstract_unit else tiled_out
          for v, tiled_out, ans_out_axes
          in zip(call_jaxpr.outvars, tiled_outs, mesh_out_axes)]

  return xops.Tuple(c, outs)

def _xla_tile_base_indices(c, axis_env, tile_shape, axes, axis_sizes):
  zero = xb.constant(c, np.zeros((), dtype=np.int32))
  linear_idxs = [zero] * len(tile_shape)
  strides = [1] * len(tile_shape)
  for name, axis in reversed(axes.items()):
    axis_index = _axis_index_translation_rule(
        c, axis_name=name, axis_env=axis_env, platform=None)
    stride_c = xb.constant(c, np.array(strides[axis], np.int32))
    if linear_idxs[axis] is zero and strides[axis] == 1:
      linear_idxs[axis] = axis_index
    else:
      linear_idxs[axis] = xops.Add(linear_idxs[axis], xops.Mul(axis_index, stride_c))
    strides[axis] *= axis_sizes[name]
  return [zero if linear_idx is zero else
          xops.Mul(linear_idx, xb.constant(c, np.array(tile_dim_size, np.int32)))
          for linear_idx, tile_dim_size in zip(linear_idxs, tile_shape)]

def _xla_tile(c, axis_env, x, in_axes, axis_sizes):
  if not in_axes:
    return x
  shape = list(c.get_shape(x).dimensions())
  tile_shape = list(shape)
  for name, axis in in_axes.items():
    axis_size = axis_sizes[name]
    assert tile_shape[axis] % axis_size == 0
    tile_shape[axis] //= axis_size
  base_idxs = _xla_tile_base_indices(c, axis_env, tile_shape, in_axes, axis_sizes)
  return xops.DynamicSlice(x, base_idxs, tile_shape)

# TODO(b/110096942): more efficient gather
def _xla_untile(c, axis_env, x, out_axes, axis_sizes, backend):
  xla_shape = c.get_shape(x)
  x_dtype = xla_shape.numpy_dtype()
  # TODO(mattjj): remove this logic when AllReduce PRED supported on CPU / GPU
  convert_bool = (np.issubdtype(x_dtype, np.bool_)
                  and xb.get_backend(backend).platform in ('cpu', 'gpu'))
  if convert_bool:
    x = xops.ConvertElementType(x, xb.dtype_to_etype(np.float32))

  tile_shape = list(xla_shape.dimensions())
  shape = list(tile_shape)
  for name, axis in out_axes.items():
    shape[axis] *= axis_sizes[name]
  base_idxs = _xla_tile_base_indices(c, axis_env, tile_shape, out_axes, axis_sizes)

  padded = xops.Broadcast(xb.constant(c, np.array(0, x_dtype)), shape)
  padded = xops.DynamicUpdateSlice(padded, x, base_idxs)
  replica_groups_protos = xc.make_replica_groups(
    xla.axis_groups(axis_env, tuple(out_axes.keys())))
  out = xops.CrossReplicaSum(padded, replica_groups_protos)

  # TODO(mattjj): remove this logic when AllReduce PRED supported on CPU / GPU
  if convert_bool:
    nonzero = xops.Ne(out, xb.constant(c, np.array(0, dtype=np.float32)))
    out = xops.ConvertElementType(nonzero, xb.dtype_to_etype(np.bool_))
  return out

def _xmap_translation_rule_spmd(c, axis_env,
                                global_in_nodes, name_stack, *,
                                call_jaxpr, name,
                                in_axes, out_axes, donated_invars,
                                global_axis_sizes,
                                spmd_in_axes, spmd_out_axes,
                                axis_resources, resource_env, backend):
  plan = EvaluationPlan.from_axis_resources(axis_resources, resource_env, global_axis_sizes)

  resource_call_jaxpr = plan.subst_axes_with_resources(call_jaxpr)
  f = lu.wrap_init(core.jaxpr_as_fun(core.ClosedJaxpr(resource_call_jaxpr, ())))
  f = hide_mapped_axes(f, in_axes, out_axes)
  f = plan.vectorize(f, in_axes, out_axes)
  mesh_in_axes, mesh_out_axes = plan.to_mesh_axes(in_axes, out_axes)
  mesh = resource_env.physical_mesh
  f = pxla.vtile_by_mesh(f, mesh, mesh_in_axes, mesh_out_axes)

  # XXX: We modify mesh_in_axes and mesh_out_axes here
  def add_spmd_axes(flat_mesh_axes: Sequence[pxla.ArrayMapping],
                    flat_extra_axes: Optional[Sequence[Sequence[Sequence[pxla.MeshAxisName]]]]):
    if flat_extra_axes is None:
      return
    for axes, extra in zip(flat_mesh_axes, flat_extra_axes):
      if extra is None:
        continue
      for dim, dim_extra in enumerate(extra):
        for dim_extra_axis in reversed(dim_extra):
          axes[dim_extra_axis] = dim
          # Not strictly necessary, but it feels right to make the outer partitioning
          # axes more major to the inner axes. Removing this makes them minor.
          axes.move_to_end(dim_extra_axis, last=False)
  add_spmd_axes(mesh_in_axes, spmd_in_axes)
  add_spmd_axes(mesh_out_axes, spmd_out_axes)
  # NOTE: We don't extend the resource env with the mesh shape, because those
  #       resources are already in scope! It's the outermost xmap that introduces
  #       them!
  global_in_avals = [core.ShapedArray(xla_type.dimensions(), xla_type.numpy_dtype())
                     for in_node in global_in_nodes
                     for xla_type in (c.get_shape(in_node),)]
  vectorized_jaxpr, global_out_avals, consts = pe.trace_to_jaxpr_final(f, global_in_avals)
  assert not consts

  global_sharding_spec = pxla.mesh_sharding_specs(mesh.shape, mesh.axis_names)
  sharded_global_in_nodes = [
    xb.set_sharding_proto(c, node, global_sharding_spec(aval, aval_axes).sharding_proto())
    if aval_axes else node
    for node, aval, aval_axes in zip(global_in_nodes, global_in_avals, mesh_in_axes)
  ]

  # We in-line here rather than generating a Call HLO as in the xla_call
  # translation rule just because the extra tuple stuff is a pain.
  global_out_nodes = xla.jaxpr_subcomp(
      c, vectorized_jaxpr, backend, axis_env, (),
      xla.extend_name_stack(name_stack, xla.wrap_name(name, 'xmap')),
      *sharded_global_in_nodes)

  sharded_global_out_nodes = [
    xb.set_sharding_proto(c, node, global_sharding_spec(aval, aval_axes).sharding_proto())
    if aval_axes else node
    for node, aval, aval_axes in zip(global_out_nodes, global_out_avals, mesh_out_axes)
  ]

  return xops.Tuple(c, sharded_global_out_nodes)


# -------- helper functions --------

def _delete_aval_axes(aval, axes: AxisNamePos):
  assert isinstance(aval, core.ShapedArray)
  shape = list(aval.shape)
  named_shape = dict(aval.named_shape)
  for name, axis in sorted(axes.items(), key=lambda x: x[1], reverse=True):
    named_shape[name] = shape[axis]
    del shape[axis]
  return aval.update(shape=tuple(shape), named_shape=named_shape)

def _insert_aval_axes(aval, axes: AxisNamePos, axis_sizes):
  assert isinstance(aval, core.ShapedArray)
  shape = list(aval.shape)
  named_shape = dict(aval.named_shape)
  for name, axis in sorted(axes.items(), key=lambda x: x[1]):
    shape.insert(axis, axis_sizes[name])
    named_shape.pop(name, None)  # The name might be missing --- it's a broadcast.
  return aval.update(shape=tuple(shape), named_shape=named_shape)


class ResourceCount(namedtuple('ResourceCount', ['nglobal', 'nlocal'])):
  def to_local(self, global_size):
    assert global_size % self.nglobal == 0, "Please report this issue!"
    return (global_size // self.nglobal) * self.nlocal

  def to_global(self, local_size):
    assert local_size % self.nlocal == 0, "Please report this issue!"
    return (local_size // self.nlocal) * self.nglobal

  @property
  def distributed(self):
    return self.nglobal != self.nlocal


def _get_axis_resource_count(axis_resources, resource_env) -> Dict[AxisName, ResourceCount]:
  global_res_shape = resource_env.shape
  local_res_shape = resource_env.local_shape
  return {axis: ResourceCount(int(np.prod(map(global_res_shape.get, resources), dtype=np.int64)),
                              int(np.prod(map(local_res_shape.get, resources), dtype=np.int64)))
          for axis, resources in axis_resources.items()}


def _get_axis_sizes(args_flat: Iterable[Any],
                    in_axes_flat: Iterable[AxisNamePos],
                    global_axis_sizes: Dict[AxisName, int],
                    axis_resource_count: Dict[AxisName, ResourceCount]):
  global_axis_sizes = dict(global_axis_sizes)
  for arg, in_axes in zip(args_flat, in_axes_flat):
    for name, dim in in_axes.items():
      resources = axis_resource_count[name]
      local_ = "local " if resources.distributed else ""
      try:
        local_dim_size = arg.shape[dim]
      except IndexError:
        # TODO(apaszke): Handle negative indices. Check for overlap too!
        raise ValueError(f"One of xmap arguments has an in_axes specification of "
                         f"{in_axes.user_repr}, which implies that it has at least "
                         f"{max(in_axes.values()) + 1} dimensions, but the argument "
                         f"has rank {arg.ndim}")
      if local_dim_size % resources.nlocal != 0:
        raise ValueError(f"One of xmap arguments has an in_axes specification of "
                         f"{in_axes.user_repr}, which implies that its size in dimension "
                         f"{dim} ({local_dim_size}) should be divisible by the number of "
                         f"{local_}resources assigned to axis {name} ({resources.nlocal})")
      global_dim_size = resources.to_global(local_dim_size)
      if name in global_axis_sizes:
        expected_local_dim_size = resources.to_local(global_axis_sizes[name])
        if local_dim_size != expected_local_dim_size:
          raise ValueError(f"The {local_}size of axis {name} was previously inferred to be "
                           f"{expected_local_dim_size}, but found an argument of shape {arg.shape} "
                           f"with in_axes specification {in_axes.user_repr}. Shape mismatch "
                           f"occurs in dimension {dim}: {local_dim_size} != {expected_local_dim_size}")
      global_axis_sizes[name] = global_dim_size
  return FrozenDict(global_axis_sizes)


def lookup_exactly_one_of(d: AxisNamePos, names: Set[AxisName]) -> Optional[int]:
  res = None
  for name in names:
    if name in d:
      if res is not None:
        raise ValueError("An input was mapped to the same resource twice")
      res = d[name]
  return res


@lu.transformation
def hide_mapped_axes(flat_in_axes, flat_out_axes, *flat_args):
  def _squeeze_mapped_axes(arg, axes: AxisNamePos):
    for dim in sorted(axes.values(), reverse=True):
      arg = arg.squeeze(dim)
    return arg

  def _unsqueeze_mapped_axes(out, axes: AxisNamePos):
    try:
      return jnp.expand_dims(out, tuple(axes.values()))
    except ValueError as e:
      # Improve the axis out of bounds errors
      # TODO(apaszke): Handle negative indices. Check for overlap too!
      if e.args[0].startswith('axis') and 'out of bounds' in e.args[0]:
        raise ValueError(f"One of xmap outputs has an out_axes specification of "
                         f"{axes.user_repr}, which requires the result of the xmapped "
                         f"function to have at least {max(axes.values()) - len(axes) + 1} "
                         f"positional dimensions, but it only has {out.ndim}")
      raise

  squeezed_args = map(_squeeze_mapped_axes, flat_args, flat_in_axes)
  flat_outputs = yield squeezed_args, {}
  yield map(_unsqueeze_mapped_axes, flat_outputs, flat_out_axes)


def _jaxpr_resources(jaxpr: core.Jaxpr, resource_env) -> Set[ResourceAxisName]:
  used_resources = set()
  for eqn in jaxpr.eqns:
    if eqn.primitive is xmap_p:
      if eqn.params['resource_env'] != resource_env:
        raise RuntimeError("Changing the resource environment (e.g. hardware mesh "
                           "spec) is not allowed inside xmap.")
      used_resources |= set(it.chain(*eqn.params['axis_resources'].values()))
    updates = core.traverse_jaxpr_params(
        partial(_jaxpr_resources, resource_env=resource_env), eqn.params)
    for update in updates:
      used_resources |= update
  return used_resources

# -------- soft_pmap --------

def soft_pmap(fun: Callable, axis_name: Optional[AxisName] = None, in_axes=0
              ) -> Callable:
  warn("soft_pmap is an experimental feature and probably has bugs!")
  _check_callable(fun)
  axis_name = core._TempAxisName(fun) if axis_name is None else axis_name

  if any(axis != 0 for axis in tree_leaves(in_axes)):
    raise ValueError(f"soft_pmap in_axes leaves must be 0 or None, got {in_axes}")
  proxy = object()
  in_axes = _replace_nones(proxy, in_axes)
  in_axes = tree_map(lambda i: {i: axis_name} if i is not proxy else {}, in_axes)


  @wraps(fun)
  def f_pmapped(*args, **kwargs):
    mesh_devices = np.array(xb.local_devices())
    with mesh(mesh_devices, ['devices']):
      return xmap(fun, in_axes=in_axes, out_axes={0: axis_name},
                  axis_resources={axis_name: 'devices'})(*args, **kwargs)
  return f_pmapped
