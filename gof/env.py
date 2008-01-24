
from copy import copy

import graph
## from value import Value, AsValue
from utils import ClsInit
from err import GofError, GofTypeError, PropagationError
from op import Op
from result import Result
from features import Listener, Orderings, Constraint, Tool
import utils

__all__ = ['InconsistencyError',
           'Env']


# class AliasDict(dict):
#     "Utility class to keep track of what Result has been replaced with what Result."

#     def group(self, main, *keys):
#         "Marks all the keys as having been replaced by the Result main."
#         keys = [key for key in keys if key is not main]
#         if self.has_key(main):
#             raise Exception("Only group results that have not been grouped before.")
#         for key in keys:
#             if self.has_key(key):
#                 raise Exception("Only group results that have not been grouped before.")
#             if key is main:
#                 continue
#             self.setdefault(key, main)

#     def ungroup(self, main, *keys):
#         "Undoes group(main, *keys)"
#         keys = [key for key in keys if key is not main]
#         for key in keys:
#             if self[key] is main:
#                 del self[key]

#     def __call__(self, key):
#         "Returns the currently active replacement for the given key."
#         next = self.get(key, None)
#         while next:
#             key = next
#             next = self.get(next, None)
#         return key



class InconsistencyError(GofError):
    """
    This exception is raised by Env whenever one of the listeners marks
    the graph as inconsistent.
    """
    pass



class Env(graph.Graph):
    """
    An Env represents a subgraph bound by a set of input results and a set of output
    results. An op is in the subgraph iff it depends on the value of some of the Env's
    inputs _and_ some of the Env's outputs depend on it. A result is in the subgraph
    iff it is an input or an output of an op that is in the subgraph.

    The Env supports the replace operation which allows to replace a result in the
    subgraph by another, e.g. replace (x + x).out by (2 * x).out. This is the basis
    for optimization in omega.

    An Env can have listeners, which are instances of EnvListener. Each listener is
    informed of any op entering or leaving the subgraph (which happens at construction
    time and whenever there is a replacement). In addition to that, each listener can
    implement the 'consistent' and 'ordering' methods (see EnvListener) in order to
    restrict how ops in the subgraph can be related.
    """

    ### Special ###

    def __init__(self, inputs, outputs, features = [], consistency_check = True): # **listeners):
        """
        Create an Env which operates on the subgraph bound by the inputs and outputs
        sets. If consistency_check is False, an illegal graph will be tolerated.
        """

        self._features = {}
        self._listeners = {}
        self._constraints = {}
        self._orderings = {}
        self._tools = {}
        
#         self._preprocessors = set()

#         for feature in features:
#             if issubclass(feature, tools.Preprocessor):
#                 preprocessor = feature()
#                 self._preprocessors.add(preprocessor)
#                 inputs, outputs = preprocessor.transform(inputs, outputs)

        # The inputs and outputs set bound the subgraph this Env operates on.
        self.inputs = set(inputs)
        self.outputs = set(outputs)
        
        for feature_class in utils.uniq_features(features):
            self.add_feature(feature_class, False)
#             feature = feature_class(self)
#             if isinstance(feature, tools.Listener):
#                 self._listeners.add(feature)
#             if isinstance(feature, tools.Constraint):
#                 self._constraints.add(feature)
#             if isinstance(feature, tools.Orderings):
#                 self._orderings.add(feature)
#             if isinstance(feature, tools.Tool):
#                 self._tools.add(feature)
#                 feature.publish()

        # All ops in the subgraph defined by inputs and outputs are cached in _ops
        self._ops = set()

        # Ditto for results
        self._results = set(self.inputs)

        # Set of all the results that are not an output of an op in the subgraph but
        # are an input of an op in the subgraph.
        # e.g. z for inputs=(x, y) and outputs=(x + (y - z),)
        self._orphans = set()

        # Maps results to ops that use them:
        # if op.inputs[i] == v then (op, i) in self._clients[v]
        self._clients = {}

        # List of functions that undo the replace operations performed.
        # e.g. to recover the initial graph one could write: for u in self.history.__reversed__(): u()
        self.history = []

        self.__import_r__(self.outputs)

        if consistency_check:
            self.validate()


    ### Public interface ###

    def add_output(self, output):
        self.outputs.add(output)
        self.__import_r__([output])

    def clients(self, r):
        "Set of all the (op, i) pairs such that op.inputs[i] is r."
        return self._clients.get(r, set())

    def checkpoint(self):
        """
        Returns an object that can be passed to self.revert in order to backtrack
        to a previous state.
        """
        return len(self.history)

    def consistent(self):
        """
        Returns True iff the subgraph is consistent and does not violate the
        constraints set by the listeners.
        """
        try:
            self.validate()
        except InconsistencyError:
            return False
        return True

    def satisfy(self, x):
        for feature_class in x.require():
            self.add_feature(feature_class)

    def add_feature(self, feature_class, do_import = True):
        if feature_class in self._features:
            return # the feature is already present
        else:
            for other_feature_class in self._features:
                if issubclass(other_feature_class, feature_class):
                    return
                elif issubclass(feature_class, other_feature_class):
                    self.__del_feature__(other_feature_class)
        self.__add_feature__(feature_class, do_import)

    def __add_feature__(self, feature_class, do_import):
        if not issubclass(feature_class, (Listener, Constraint, Orderings, Tool)):
            raise TypeError("features must be subclasses of Listener, Constraint, Orderings and/or Tools")
        feature = feature_class(self)
        if issubclass(feature_class, Listener):
            self._listeners[feature_class] = feature
            if do_import:
                for op in self.io_toposort():
                    feature.on_import(op)
        if issubclass(feature_class, Constraint):
            self._constraints[feature_class] = feature
        if issubclass(feature_class, Orderings):
            self._orderings[feature_class] = feature
        if issubclass(feature_class, Tool):
            self._tools[feature_class] = feature
            feature.publish()
        self._features[feature_class] = feature

    def __del_feature__(self, feature_class):
        for set in [self._features, self._constraints, self._orderings, self._tools, self._listeners]:
            try:
                del set[feature_class]
            except KeyError:
                pass

#         for i, feature in enumerate(self._features):
#             if isinstance(feature, feature_class): # exact class or subclass, nothing to do
#                 return
#             elif issubclass(feature_class, feature.__class__): # superclass, we replace it
#                 new_feature = feature_class(self)
#                 self._features[i] = new_feature
#                 break
#         else:
#             new_feature = feature_class(self)
#             self._features.append(new_feature)
#         if isinstance(new_feature, tools.Listener):
#             for op in self.io_toposort():
#                 new_feature.on_import(op)

    def get_feature(self, feature_class):
        try:
            return self._features[feature_class]
        except KeyError:
            for other_feature_class in self._features:
                if issubclass(other_feature_class, feature_class):
                    return self._features[other_feature_class]
            else:
                raise

    def has_feature(self, feature_class):
        try:
            self.get_feature(feature_class)
        except:
            return False
        return True

    def nclients(self, r):
        "Same as len(self.clients(r))."
        return len(self.clients(r))

    def ops(self):
        "All ops within the subgraph bound by env.inputs and env.outputs."
        return self._ops

    def has_op(self, op):
        return op in self._ops

    def orphans(self):
        """All results not within the subgraph bound by env.inputs and env.outputs, not in
        env.inputs but required by some op."""
        return self._orphans

    def replace(self, r, new_r, consistency_check = True):
        """
        This is the main interface to manipulate the subgraph in Env.
        For every op that uses r as input, makes it use new_r instead.
        This may raise a GofTypeError if the new result violates type
        constraints for one of the target ops. In that case, no
        changes are made.

        If the replacement makes the graph inconsistent and the value
        of consistency_check is True, this function will raise an
        InconsistencyError and will undo the operation, leaving the
        graph the way it was before the call to replace.

        If consistency_check is False, the replacement will succeed
        even if there is an inconsistency. A GofTypeError will still
        be raised if there are type mismatches.
        """

        # Assert that they are Result instances.
        assert isinstance(r, Result)
        assert isinstance(new_r, Result)

        # Save where we are so we can backtrack
        if consistency_check:
            chk = self.checkpoint()

        # The copy is required so undo can know what clients to move back!
        clients = copy(self.clients(r))

        # Messy checks so we know what to do if we are replacing an output
        # result. Note that if v is an input result, we do nothing at all for
        # now (it's not clear what it means to replace an input result).
        was_output = False
        new_was_output = False
        if new_r in self.outputs:
            new_was_output = True
        if r in self.outputs:
            was_output = True
            self.outputs.remove(r)
            self.outputs.add(new_r)

        # The actual replacement operation occurs here. This might raise
        # a GofTypeError
        self.__move_clients__(clients, r, new_r)

        # This function undoes the replacement.
        def undo():
            # Restore self.outputs
            if was_output:
                if not new_was_output:
                    self.outputs.remove(new_r)
                self.outputs.add(r)

            # Move back the clients. This should never raise an error.
            self.__move_clients__(clients, new_r, r)

        self.history.append(undo)
        
        if consistency_check:
            try:
                self.validate()
            except InconsistencyError, e:
                self.revert(chk)
                raise

    def replace_all(self, d):
        """
        For (r, new_r) in d.items(), replaces r with new_r. Checks for consistency at the
        end and raises an InconsistencyError if the graph is not consistent. If an error is
        raised, the graph is restored to what it was before.
        """
        chk = self.checkpoint()
        try:
            for r, new_r in d.items():
                self.replace(r, new_r, False)
        except Exception, e:
            self.revert(chk)
            raise
        try:
            self.validate()
        except InconsistencyError, e:
            self.revert(chk)
            raise

    def results(self):
        "All results within the subgraph bound by env.inputs and env.outputs and including them"
        return self._results

    def revert(self, checkpoint):
        """
        Reverts the graph to whatever it was at the provided checkpoint (undoes all replacements).
        A checkpoint at any given time can be obtained using self.checkpoint().
        """
        while len(self.history) > checkpoint:
            f = self.history.pop()
            f()

    def supplemental_orderings(self):
        ords = {}
        for ordering in self._orderings.values():
            for op, prereqs in ordering.orderings().items():
                ords.setdefault(op, set()).update(prereqs)
        return ords

    def toposort(self):
        """
        Returns a list of ops in the order that they must be executed in order to preserve
        the semantics of the graph and respect the constraints put forward by the listeners.
        """
        ords = self.supplemental_orderings()
        order = graph.io_toposort(self.inputs, self.outputs, ords)
        return order
    
    def validate(self):
        for constraint in self._constraints.values():
            constraint.validate()
        return True


    ### Private interface ###

    def __add_clients__(self, r, all):
        self._clients.setdefault(r, set()).update(all)

    def __remove_clients__(self, r, all):
        if not all:
            return
        self._clients[r].difference_update(all)
        if not self._clients[r]:
            del self._clients[r]

    def __import_r__(self, results):
        for result in results:
            owner = result.owner
            if owner:
                self.__import__(result.owner)

    def __import__(self, op):
        # We import the ops in topological order. We only are interested
        # in new ops, so we use all results we know of as if they were the input set.
        # (the functions in the graph module only use the input set to
        # know where to stop going down)
        new_ops = graph.io_toposort(self.results(), op.outputs)
        
        for op in new_ops:
            self.satisfy(op) # add the features required by this op
            
            self._ops.add(op)
            self._results.update(op.outputs)
            
            for i, input in enumerate(op.inputs):
                self.__add_clients__(input, [(op, i)])
                if input not in self._results:
                    # This input is an orphan because if the op that
                    # produced it was in the subgraph, io_toposort
                    # would have placed it before, so we would have
                    # seen it (or it would already be in the graph)
                    self._orphans.add(input)
                    self._results.add(input)
            
            for listener in self._listeners.values():
                listener.on_import(op)

    def __prune_r__(self, results):
        for result in set(results):
            if result in self.inputs:
                continue
            owner = result.owner
            if owner:
                self.__prune__(owner)

    def __prune__(self, op):
        for output in op.outputs:
            # Cannot prune an op which is an output or used somewhere
            if self.clients(output) or output in self.outputs: #output in self.outputs or self.clients(output):
                return
        self._ops.remove(op)
        self._results.difference_update(op.outputs)
        
        for listener in self._listeners.values():
            listener.on_prune(op)
            
        for i, input in enumerate(op.inputs):
            self.__remove_clients__(input, [(op, i)])
        self.__prune_r__(op.inputs)

    def __move_clients__(self, clients, r, new_r):
        try:
            # Try replacing the inputs
            for op, i in clients:
                op.set_input(i, new_r, False)
        except GofTypeError, PropagationError:
            # Oops!
            for op, i in clients:
                op.set_input(i, r, False)
            raise
        self.__remove_clients__(r, clients)
        self.__add_clients__(new_r, clients)

        # We import the new result in the fold
        self.__import_r__([new_r])
        
        for listener in self._listeners.values():
            listener.on_rewire(clients, r, new_r)

        # We try to get rid of the old one
        self.__prune_r__([r])

    def __str__(self):
        return graph.as_string(self.inputs, self.outputs)


