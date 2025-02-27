from __future__ import print_function

import ctypes
import time

from . import bindings as cweld
from .types import *


class WeldObjectEncoder(object):
    """An abstract class that must be overridden by libraries. This class
    is used to marshall objects from Python types to Weld types."""

    def encode(self, obj):
        """Encodes an object. All objects encode-able by this encoder should return
        a valid Weld type using py_to_weld_type."""
        raise NotImplementedError

    def py_to_weld_type(self, obj):
        """Returns a WeldType corresponding to a Python object."""
        raise NotImplementedError


class WeldObjectDecoder(object):
    """An abstract class that must be overridden by libraries. This class
    is used to marshall objects from Weld types to Python types."""
    def decode(self, obj, restype):
        """Decodes obj, assuming object is of type `restype`. obj's Python
        type is ctypes.POINTER(restype.ctype_class)."""
        raise NotImplementedError


class WeldObject(object):
    """Holds a Weld program to be lazily compiled and evaluated,
    along with any context required to evaluate the program.

    Libraries that use the Weld API return WeldObjects, which represent
    "lazy executions" of programs. WeldObjects can build on top of each other,
    i.e. libraries should be implemented so they can accept both their
    native types and WeldObjects.

    An WeldObject contains a Weld program as a string, along with a context.
    The context maps names in the Weld program to concrete values.
    When a WeldObject is evaluated, it uses its encode and decode functions to
    marshall native library types into types that Weld understands. The basic
    flow of evaluating a Weld expression is thus:
    1. "Finish" the Weld program by adding a function header
    2. Compile the Weld program and load it as a dynamic library
    3. For each argument, run object.encoder on each argument. See
       WeldObjectEncoder for details on how this works
    4. Pass the encoded arguments to Weld
    5. Run the decoder on the return value. See WeldObjectDecoder for details
       on how this works.
    6. Return the decoded value.
    """

    # Counter for assigning variable names
    _var_num = 0
    _obj_id = 100
    _registry = {}

    def __init__(self, encoder, decoder):
        self.encoder = encoder
        self.decoder = decoder

        # Weld program
        self.weld_code = ""
        self.dependencies = {}

        # Assign a unique ID to the context
        self.obj_id = "obj%d" % WeldObject._obj_id
        WeldObject._obj_id += 1

        # Maps name -> input data
        self.context = {}
        # Maps name -> arg type (for arguments that don't need to be encoded)
        self.argtypes = {}

    def __repr__(self):
        return self.weld_code + " " + str(self.context) + " " + str([obj_id for obj_id in self.dependencies])

    @staticmethod
    def generate_input_name(value_str):
        name = "_inp%d" % WeldObject._var_num
        WeldObject._var_num += 1
        WeldObject._registry[value_str] = name

        return name

    def update(self, value, tys=None, override=True):
        """Update this context.

        If value is another context,
        the names from that context are added into this one.
        Otherwise, a new name is assigned and returned.
        TODO tys for inputs.
        """
        from baloo.weld import LazyResult
        if isinstance(value, WeldObject):
            self.context.update(value.context)
        elif isinstance(value, LazyResult):
            self.context.update(value.weld_expr.context)
        else:
            # Ensure that the same inputs always have same names
            value_str = str(value)
            if value_str in WeldObject._registry:
                name = WeldObject._registry[value_str]
            else:
                name = WeldObject.generate_input_name(value_str)
            self.context[name] = value
            if tys is not None and not override:
                self.argtypes[name] = tys

            return name

    def get_let_statements(self):
        queue = [self]
        visited = set()
        let_statements = []
        while len(queue) > 0:
            cur_obj = queue.pop()
            cur_obj_id = cur_obj.obj_id
            if cur_obj_id not in visited:
                let_statements.insert(0, "let %s = (%s);" % (cur_obj_id, cur_obj.weld_code))
                for key in sorted(cur_obj.dependencies.keys()):
                    queue.append(cur_obj.dependencies[key])
                visited.add(cur_obj_id)
        let_statements.sort()  # To ensure that let statements are in the right order in the final generated program
        let_statements.insert(len(let_statements), self.obj_id)

        return "\n".join(let_statements)

    def to_weld_func(self):
        names = sorted(self.context.keys())
        arg_strs = ["{0}: {1}".format(str(name),
                                      str(self.encoder.py_to_weld_type(self.context[name])))
                    for name in names]
        header = "|" + ", ".join(arg_strs) + "|"
        text = header + " " + self.get_let_statements() + "\n" + self.weld_code

        return text

    def generate(self):
        return self.get_let_statements()

    def evaluate(self, restype, verbose=True, decode=True, passes=None,
                 num_threads=1, apply_experimental_transforms=False):
        func = self.to_weld_func()

        # Returns a wrapped ctypes Structure
        def args_factory(encoded_fields):
            class Args(ctypes.Structure):
                _fields_ = [e for e in encoded_fields]

            return Args

        # Encode each input argument. This is the positional argument list
        # which will be wrapped into a Weld struct and passed to the Weld API.
        names = sorted(self.context.keys())

        start = time.time()
        encoded = []
        argtypes = []
        for name in names:
            if name in self.argtypes:
                argtypes.append(self.argtypes[name].ctype_class)
                encoded.append(self.context[name])
            else:
                argtypes.append(self.encoder.py_to_weld_type(
                    self.context[name]).ctype_class)
                encoded.append(self.encoder.encode(self.context[name]))
        end = time.time()

        if verbose:
            print("Python->Weld:", end - start)

        args = args_factory(zip(names, argtypes))
        weld_args = args()
        for name, value in zip(names, encoded):
            setattr(weld_args, name, value)

        start = time.time()
        void_ptr = ctypes.cast(ctypes.byref(weld_args), ctypes.c_void_p)
        arg = cweld.WeldValue(void_ptr)
        conf = cweld.WeldConf()
        err = cweld.WeldError()

        if passes is not None:
            passes = ",".join(passes)
            passes = passes.strip()
            if passes != "":
                conf.set("weld.optimization.passes", passes)

        module = cweld.WeldModule(func, conf, err)
        if err.code() != 0:
            raise ValueError("Could not compile function {}: {}".format(
                func, err.message()))
        end = time.time()

        if verbose:
            print("Weld compile time:", end - start)

        start = time.time()
        conf = cweld.WeldConf()
        conf.set("weld.threads", str(num_threads))
        conf.set("weld.memory.limit", "100000000000")
        conf.set("weld.optimization.applyExperimentalTransforms",
                 "true" if apply_experimental_transforms else "false")
        err = cweld.WeldError()
        weld_ret = module.run(conf, arg, err)
        if err.code() != 0:
            raise ValueError(("Error while running function,\n{}\n\n"
                              "Error message: {}").format(
                func, err.message()))
        ptrtype = POINTER(restype.ctype_class)
        data = ctypes.cast(weld_ret.data(), ptrtype)
        end = time.time()

        if verbose:
            print("Weld:", end - start)

        start = time.time()
        if decode:
            result = self.decoder.decode(data, restype)
        else:
            data = cweld.WeldValue(weld_ret).data()
            result = ctypes.cast(data, ctypes.POINTER(
                ctypes.c_int64)).contents.value
        end = time.time()

        if verbose:
            print("Weld->Python:", end - start)

        return result
