"""
Module to trace and document the training and execution of a BiFlow.

The BiNetTraceInspector class is used to decorate a flow for inspection, the
snapshots are created with the TraceBiNetHTMLTranslator class.

This module also supports (Bi)HiNet structures. Monkey patching is used to
inject the tracing code into the Flow.
"""

import os
import new
import cPickle as pickle
import fnmatch
import traceback
import copy

import numpy as n

import mdp.hinet as hinet

from ..binode import BiNode
from ..biflow import BiFlow
from ..bihinet import BiFlowNode, CloneBiLayer

from bihinet_translator import BiNetHTMLTranslator, NODE_HTML_TRANSLATORS
from utils import robust_pickle

# TODO: wrap inner methods (e.g. _train) to document effective arguments?
# TODO: pretty print numpy arrays 

PICKLE_EXT = ".pckl" 
PICKLE_PROTO = -1
SNAPSHOT_FILENAME = "snapshot"

SLIDE_CSS_FILENAME = "inspect.css"

NODE_TRACE_METHOD_NAMES = ["execute", "train", "stop_training"]
BINODE_TRACE_METHOD_NAMES = ["message", "stop_message", "global_message"]
TRACING_WRAP_FLAG = "_is_wrapped_for_tracing"

# additions to the BINET_STYLE for marking the currently traced nodes
INSPECT_TRACE_STYLE = """
#current_node {
    background-color: #D1FFC7;
}

#current_branch_base_node {
    background-color: #E1FFDD;
}

#inspect_biflow_td {
    vertical-align: top;
    padding: 0 100 0 0;
}

#inspect_result_td {
    vertical-align: top;
}

#displayed {
    border-top: 1px solid #003399;
}

table.inspect_io_data {
    font-family: monospace;
}

table.inspect_io_data td {
    vertical-align: top;
}

table.inspect_io_data pre {
    font-weight: bold;
}

span.inactive_section {
    color: #0000EE;
    cursor: pointer;
    font-weight: bold;
}

div.error {
    color: #FF0000;
}

div.error h3 {
    font-size: large;
    color: #FF0000;
}

html {
    overflow-y : scroll;
}
"""

class HTMLTraceTranslator(object):
    """Abstract base class which is used by HTMLTraceInspector."""
    
    def write_flow_to_file(self, flow, html_file, node=None, method_name=None, 
                           result=None, args=None, kwargs=None,
                           branch_base_node=None):
        """Write the visualization of the current status into the html_file.""" 
        pass


class BiNetTraceDebugException(Exception):
    """Exception for return the information when debug is True."""
    
    def __init__(self, result, exception):
        """Store the information necessary to finish the tracing.
        
        result -- The result that would otherwise be returned by the method.
        exception -- The original caufght exception.
        """
        self.result = result
        self.exception = exception


class HTMLTraceInspector(hinet.HiNetTranslator):
    """Class for inspecting a single pass through a provided flow.
    
    This class is based on a translator that wraps the flow elements with 
    tracing decorators. It also provides a callback function for the tracers
    and stores everything else needed for the inspection.
    
    This class is already specialized for creating HTML slides in the callback
    function.
    
    Note that a flow decorated for tracing is not compatible with pickling
    or parallel training and execution. But normally the decorated flow is
    only used in trace_training or trace_execution anyway.
    """
    
    def __init__(self, trace_translator, css_filename=SLIDE_CSS_FILENAME):
        """Prepare for tracing and create the HTML translator.
        
        trace_translator -- HTMLTraceTranslator instance, with a write method
            to create the status visualization on each slide.
        css_filename -- CSS file used for all the slides.
        """
        super(HTMLTraceInspector, self).__init__()
        self._css_filename = css_filename
        self._trace_path = None  # path for the current trace 
        self._trace_name = None  # name for the current trace 
        self._flow = None  # needed for the callback HTML translation
        self.trace_translator = trace_translator
        # step counter used in the callback, is reset automatically
        self._slide_index = 0
        self._slide_filenames = []
        self._section_ids = []  # can be used during execution
        self._undecorate_mode = False  # if True undecorate nodes
        self._branch_base_state = None  # used for branching
        
    def _reset_variables(self):
        """Reset the internal variables for new tracing."""
        self._slide_index = 0
        self._slide_filenames = []
        self._section_ids = []
        self._branch_base_state = None
        self._undecorate_mode = False 
        
    def trace_training(self, path, flow, x, msg=None, stop_msg=None,
                       trace_name="training", debug=False):
        """Trace a single training phase and the stop_training.
        
        Return a tuple containing a list of the training slide filenames and
        a list of the the stop_training filenames.
        
        path -- Path were the inspection files will be stored.
        trace_name -- Name prefix for this inspection (default is training).
        """
        self._reset_variables()
        self._trace_path = path
        # train and stop filenames must be different
        self._trace_name = trace_name + "_t"
        self._flow = flow
        self._translate_flow(flow)
        biflownode = BiFlowNode(BiFlow(flow.flow))
        try:
            biflownode.train(x=x, msg=msg)
        except Exception, exception:
            if debug:
                # insert the error slide and encapsulate the exception
                self._write_error_frame(exception)
                result = (self._slide_filenames, None)
                raise BiNetTraceDebugException(result=result,
                                               exception=exception) 
            else:
                traceback.print_exc()
                err = "Exception during training trace."
                raise Exception(err)
        train_filenames = self._slide_filenames
        self._reset_variables()
        self._trace_name = trace_name + "_s"
        try:
            biflownode.stop_training(stop_msg)
        except Exception, exception:
            if debug:
                # insert the error slide and encapsulate the exception
                self._write_error_frame(exception)
                result = (train_filenames, self._slide_filenames)
                raise BiNetTraceDebugException(result=result,
                                               exception=exception) 
            else:
                traceback.print_exc()
                raise exception
        stop_filenames = self._slide_filenames
        # restore undecoreted flow
        self._undecorate_mode = True
        self._translate_flow(flow)
        return train_filenames, stop_filenames
    
    def trace_execution(self, path, trace_name, flow, x, msg=None,
                        debug=False):
        """Trace a single execution.
        
        The return value is a tuple containing a list of the slide filenames,
        the section_ids for a slideshow with sections (the second part is
        None if no section_ids were used) and the execution output value.
        
        path -- Path were the inspection files will be stored.
        trace_name -- Name prefix for this inspection.
        """
        self._reset_variables()
        self._trace_path = path
        self._trace_name = trace_name
        self._flow = flow
        self._translate_flow(flow)
        if (not isinstance(flow, BiFlow)) and (msg is not None):
            # a msg would be interpreted as nodenr by a Flow, so check this
            err = "A msg was given for a normal Flow (need BiFlow)."
            raise Exception(err)
        try:
            if msg is None:
                y = self._flow.execute(x)
            else:
                y = self._flow.execute(x, msg)
        except Exception, exception:
            if debug:
                self._write_error_frame(exception)
                if not self._section_ids:
                    self._section_ids = None
                result = (self._slide_filenames, self._section_ids)
                raise BiNetTraceDebugException(result=result,
                                               exception=exception) 
            else:
                traceback.print_exc()
                raise exception
        self._undecorate_mode = True
        self._translate_flow(flow)
        if not self._section_ids:
            self._section_ids = None
        else:
            if len(self._section_ids) != len(self._slide_filenames):
                err = ("Mismatch between number of section_ids and number of "
                       "slides.")
                raise Exception(err)
        return self._slide_filenames, self._section_ids, y 
    
    def _tracer_callback(self, node, method_name, result, args, kwargs):
        """This method is called by the tracers.
        
        The calling tracer also provides this method with the needed state
        information and the method arguments.
        
        node -- The node from which the callback was initiated.
        method_name -- Name of the method from which the callback was initiated.
        result -- Return value of the method.
        args, kwargs -- The arguments of the method call.
        """
        ## store data to be prepared for branch,
        branch_base_node = None
        if method_name in NODE_TRACE_METHOD_NAMES:
            # a message after training or stop_training is here also
            # considered as a branch and the base node gets highlighted
            self._branch_base_state = (node, method_name, result)
        elif method_name in BINODE_TRACE_METHOD_NAMES:
            # deal with message branch
            if self._branch_base_state is not None:
                branch_base_node = self._branch_base_state[0]
        ## write visualization to html_file
        try:
            html_file = self._begin_HTML_frame()
            section_id = self.trace_translator.write_flow_to_file(
                                            path=self._trace_path,
                                            html_file=html_file, 
                                            flow=self._flow,
                                            node=node,
                                            method_name=method_name, 
                                            result=result,
                                            args=args,
                                            kwargs=kwargs,
                                            branch_base_node=branch_base_node)
            self._slide_index += 1
            if section_id is not None:
                self._section_ids.append(section_id)
                
        finally:
            self._end_HTML_frame(html_file)
    
    ## HTML decoration ##
    
    def _begin_HTML_frame(self):
        """Return the HTML file for a trace frame including the header.
        
        The file should then be finished via _end_HTML_frame.
        """
        path = self._trace_path
        filename = self._trace_name + "_%d.html" % self._slide_index
        self._slide_filenames.append(filename)
        html_file = open(os.path.join(path, filename), "w")
        html_file = hinet.NewlineWriteFile(html_file)
        html_file.write('<html>\n<head>\n<title>Inspection Slide</title>')
        if self._css_filename:
            html_file.write('<style type="text/css" media="screen">')
            html_file.write('@import url("%s");' % self._css_filename)
            html_file.write('</style>\n</head>\n<body>')
        return html_file
    
    def _end_HTML_frame(self, html_file):
        """Complete and close the HTML file for a trace frame.
        
        The method should always be used after _begin_HTML_frame.
        """
        html_file.write('</body>\n</html>')
        html_file.close()
        
    def _write_error_frame(self, exception):
        html_file = self._begin_HTML_frame()
        html_file.write('<div class="error">')
        html_file.write('<h3>Encountered Exception</h3>')
        html_file.write(str(exception))
        html_file.write('</div>')
        self._end_HTML_frame(html_file)
        
    ## translation methods ##
    
    # note that via the _undecorate_mode flag the translation undecorates,
    # this is required for clonelayer, where only one instance is decorated
    
    def _translate_flow(self, flow):
        """Wrap the flow in place and return it."""
        super(HTMLTraceInspector, self)._translate_flow(flow)
        return flow
    
    # TODO: enable the use of a shallow copy to save memory,
    #    but this requires to implement __copy__ in Node etc. for recursive
    #    shallow copying
    
    def _translate_clonelayer(self, clonelayer):
        """Add a special wrapper for switch_to_copies."""
        if self._undecorate_mode:
            if isinstance(clonelayer, CloneBiLayer):
                # check that clonelayer is actually decorated
                if not hasattr(clonelayer, "_original_set_use_copies"):
                    return
                del clonelayer._set_use_copies
                del clonelayer._original_set_use_copies
                del clonelayer.__getstate__
                self._translate_node(clonelayer.nodes[0])
                if not clonelayer.use_copies:
                    clonelayer.nodes = ((clonelayer.node,) *
                                        len(clonelayer.nodes))
            else:
                self._translate_node(clonelayer.nodes[0])
                clonelayer.nodes = (clonelayer.node,) * len(clonelayer.nodes)
            # undecoration is complete
            return
        ## decorate clonelayer
        if ((not isinstance(clonelayer, CloneBiLayer)) or
            (not clonelayer.use_copies)):
            # use a decorated deep copy for the first node
            clonelayer.node = clonelayer.nodes[0].copy()
            clonelayer.nodes = (clonelayer.node,) + clonelayer.nodes[1:]
        # only decorate the first node
        self._translate_node(clonelayer.nodes[0])
        if isinstance(clonelayer, CloneBiLayer):
            # add a wrapper to _set_use_copies, 
            # otherwise all nodes in layer would get decorated
            clonelayer._original_set_use_copies = clonelayer._set_use_copies
            trace_inspector = self
            def wrapped_use_copies(self, use_copies):
                # undecorate internal nodes to allow copy operation
                trace_inspector._undecorate_mode = True
                trace_inspector._translate_node(clonelayer.nodes[0])
                trace_inspector._undecorate_mode = False
                if use_copies and not self.use_copies:
                    # switch to node copies, no problem
                    clonelayer._original_set_use_copies(use_copies)
                elif not use_copies and self._use_copies:
                    # switch to a single node instance
                    # but use a (decorated) deep copy for first node
                    clonelayer._original_set_use_copies(use_copies)
                    clonelayer.node = clonelayer.nodes[0].copy()
                    clonelayer.nodes = (clonelayer.node,) + clonelayer.nodes[1:]
                trace_inspector._translate_node(clonelayer.nodes[0])
            clonelayer._set_use_copies = new.instancemethod(wrapped_use_copies, 
                                                            clonelayer)
            # modify getstate to enable pickling (get rid of the instance methods)
            def wrapped_getstate(self):
                result = self.__dict__.copy()
                # delete instance methods
                del result["_original_set_use_copies"]
                del result["_set_use_copies"]
                del result["__getstate__"]
                return result
            clonelayer.__getstate__ = new.instancemethod(wrapped_getstate,
                                                         clonelayer)
        
    def _translate_standard_node(self, node):
        """Wrap the node."""
        if not self._undecorate_mode:
            self._tracer_decorate(node)
        else:
            self._tracer_undecorate(node)
        
    ## monkey patching methods ##
        
    def _tracer_decorate(self, node):
        """Adds a tracer wrapper to the node via monkey patching."""
        # add a marker to show that this node is wrapped
        setattr(node, TRACING_WRAP_FLAG, True) 
        trace_method_names = list(NODE_TRACE_METHOD_NAMES)
        if isinstance(node, BiNode):
            trace_method_names += BINODE_TRACE_METHOD_NAMES
        for method_name in trace_method_names:
            new_method_name = "_original_" + method_name
            # create a reference to the original method
            setattr(node, new_method_name, getattr(node, method_name))
            # use nested scopes  lexical closure to get proper wrapper
            def get_wrapper(_method_name, _inspector):
                _new_method_name = "_original_" + method_name
                def wrapper(self, *args, **kwargs):
                    args_copy = copy.deepcopy(args)
                    kwargs_copy = copy.deepcopy(kwargs)
                    result = getattr(self, _new_method_name)(*args, **kwargs)
                    _inspector._tracer_callback(self, _method_name, result,
                                                args_copy, kwargs_copy)
                    return result
                return wrapper
            # hide the original method in this instance behind the wrapper
            setattr(node, method_name, 
                    new.instancemethod(get_wrapper(method_name, self), node))
        # modify getstate to enable pickling (get rid of the instance methods)
        def wrapped_getstate(self):
            result = self.__dict__.copy()
            if not hasattr(node, TRACING_WRAP_FLAG):
                return result
            del result[TRACING_WRAP_FLAG]
            # delete all instance methods
            trace_method_names = list(NODE_TRACE_METHOD_NAMES)
            if isinstance(self, BiNode):
                trace_method_names += BINODE_TRACE_METHOD_NAMES
            for method_name in trace_method_names:
                del result[method_name]
                old_method_name = "_original_" + method_name
                del result[old_method_name]
            del result["__getstate__"]
            return result
        node.__getstate__ = new.instancemethod(wrapped_getstate, node)
    
    def _tracer_undecorate(self, node):
        """Remove a tracer wrapper from the node."""
        if not hasattr(node, TRACING_WRAP_FLAG):
            return
        delattr(node, TRACING_WRAP_FLAG)
        trace_method_names = list(NODE_TRACE_METHOD_NAMES)
        if isinstance(node, BiNode):
            trace_method_names += BINODE_TRACE_METHOD_NAMES
        for method_name in trace_method_names:
            # delete the wrapped method in the instance to unhide the original
            delattr(node, method_name)
            # delete the no longer used reference to the original method
            old_method_name = "_original_" + method_name
            delattr(node, old_method_name)
        # restore normal getstate
        delattr(node, "__getstate__")


class TraceBiNetHTMLTranslator(HTMLTraceTranslator, BiNetHTMLTranslator):
    """Class to visualize the state of a BiFlow during execution or training.
    
    The single snapshoot is a beefed up version of the standard HTML view.
    Capturing the data to make this possible is not the responsibility of this
    class.
    """
    
    def __init__(self, node_param_translators=NODE_HTML_TRANSLATORS,
                 show_size=False):
        """Initialize the internal variables."""
        super(TraceBiNetHTMLTranslator, self).__init__(
                                node_param_translators=node_param_translators,
                                show_size=show_size)
        self._current_node = None
        self._method_name = None
        self._result = None
        self._branch_node = None
    
    def write_flow_to_file(self, path, html_file, flow, node, method_name, 
                           result, args, kwargs, branch_base_node):
        """Write the HTML translation of the flow into the provided file.
        
        path -- Path of the slide (e.h. to store additional images).
        html_file -- File of current slide, where the translation is written.
        flow -- The overall flow.
        node -- The node that was called last.
        method_name -- The method that was called on the last node.
        result -- The result from the last call.
        args -- args that were given to the method
        kwargs -- kwargs that were given to the method
        branch_base_node -- Node from which the current branch started.
        """
        self._html_file = hinet.NewlineWriteFile(html_file)
        f = self._html_file
        ## create table, left side for the flow, right side for data
        f.write('<br><br>')
        f.write('<table><tr><td id="inspect_biflow_td">')
        f.write("<h3>flow state</h3>")
        self._translate_flow(flow, node, branch_base_node)
        # now the argument / result part of the table
        f.write('</td><td id="inspect_result_td">')
        section_id = self._write_right_side(
                               path=path, html_file=html_file, flow=flow,
                               node=node, method_name=method_name,
                               result=result, args=args, kwargs=kwargs,
                               branch_base_node=branch_base_node)
        f.write('</table>')
        f.write('</td></tr>\n</table>')
        self._html_file = None
        return section_id
        
    def _write_right_side(self, path, html_file, flow, node, method_name, 
                          result, args, kwargs, branch_base_node):
        """Write the result part of the translation.
        
        Return value is None, but could be section_id.
        """
        f = self._html_file
        if not method_name == "stop_training":
            f.write('<h3>%s arguments</h3>' % method_name)
            f.write('<table class="inspect_io_data">')
            if (method_name in ["execute", "train"]) and args:
                # deal with x separately
                x = args[0]
                args = args[1:]
                if isinstance(x, n.ndarray):
                    f.write('<tr><td><pre>x = </pre></td>' +
                            '<td>(shape: %s)' % str(x.shape) + str(x) +
                            '</td></tr>')
                else:
                    f.write('<tr><td><pre>x = </pre></td>' + '<td>' + str(x) +
                            '</td></tr>')
            if args:
                f.write('<tr><td><pre>msg = </pre></td>' + '<td>' +
                        str(args[0]) + '</td></tr>')
            # normally the kwargs should be empty
            for arg_key in kwargs:
                f.write('<tr><td><pre>' + arg_key + ' = </pre></td><td>' + 
                        str(kwargs[arg_key]) + '</td></tr>')
            f.write('</table>')
        ## print results
        f.write("<h3>%s result</h3>" % method_name)
        f.write('<table class="inspect_io_data">')
        if result is None:
            f.write('<tr><td><pre>None</pre></tr></td>')
        elif isinstance(result, n.ndarray):
            f.write('<tr><td><pre>x = </pre></td>' +
                    '<td>(shape: %s)' % str(result.shape) + str(result) +
                    '</td></tr>')
        elif isinstance(result, dict):
            f.write('<tr><td><pre>msg = </pre></td><td>' + str(result) +
                    '</td></tr>')
        elif isinstance(result, tuple):
            # interpret the results depending on the method name
            if method_name == "execute":
                result_names = ["x", "msg", "target", "branch_msg",
                                "branch_target"]
            elif method_name == "train":
                if len(result) == 2:
                    result_names = ["msg", "target"]
                else:
                    result_names = ["x", "msg", "target", "branch_msg",
                                    "branch_target"]
            else:
                result_names = ["msg", "target"]
            for i_result_part, result_part in enumerate(result):
                f.write('<tr><td><pre>' + result_names[i_result_part] +
                        ' = </pre></td><td>')
                if isinstance(result_part, n.ndarray):
                    f.write('(shape: %s)' % str(result_part.shape) +
                            str(result_part) + '</td></tr>')
                else:
                    f.write(str(result_part) + '</td></tr>')
        else:
            f.write('<tr><td><pre>unknown result type: </pre></td><td>' +
                    str(result) + '</td></tr>')
        
    # overwrite private methods
    
    def _translate_flow(self, flow, current_node=None, branch_base_node=None):
        """Translate the flow into HTML and write it into the internal file.
        
        Use write_flow_to_file instead of calling this method directly.
        This method only takes care of the BiFlow structure graph (the left
        part of the slide).
        
        current_node -- The current_node that was called last.
        branch_base_node -- Node from which the current bi_message branch
            started.
            
        These arguments are stored as attributes and are then used in
        _open_node_env.
        """
        self._current_node = current_node
        self._branch_base_node = branch_base_node
        super(TraceBiNetHTMLTranslator, self)._translate_flow(flow)
        
    def _open_node_env(self, node, type_id="node"):
        """Open the HTML environment for the node internals.
        
        This special version highlights the nodes involved in the trace.
        
        node -- The node itself.
        type_id -- The id string as used in the CSS.
        """
        f = self._html_file
        trace_id = None 
        if node is self._current_node:
            trace_id = "current_node"
        elif node is self._branch_base_node:
            trace_id = "current_branch_base_node"
        if trace_id:
            f.write('<table id=%s class="%s">' % (trace_id, type_id))
        else:    
            f.write('<table class="%s">' % type_id)
        self._write_node_header(node, type_id)


## Functions to capture pickled biflow snapshots during training. ##

def prepare_training_inspection(flow, path):
    """Use hook in the BiFlow to store a snapshot in each training phase.
    
    path -- Path were the snapshots are stored.
    
    This is done by wrapping the _stop_training_hook method of biflow.
    Some attributes are added to the biflow which store all information needed
    for the pickling (like filename). To enable pickling we use the
    __getstate__ slot, since some attributes cannot be pickled.
    """
    # add attributes to biflow which are used in wrapper_method
    flow._snapshot_counter_ = 0
    flow._snapshot_path_ = path
    flow._snapshot_name_ = SNAPSHOT_FILENAME
    flow._snapshot_instance_methods_ = []
    ### wrap _stop_training_hook to store biflow snapshots ###
    def pickle_wrap_method(_flow, _method_name):
        new_method_name = "_original_" + _method_name
        def wrapper(self, *args, **kwargs):
            result = getattr(self, new_method_name)(*args, **kwargs)
            # pickle biflow
            filename = (self._snapshot_name_ + "_%d" % self._snapshot_counter_ + 
                        PICKLE_EXT)
            robust_pickle(self._snapshot_path_, filename, self)
            self._snapshot_counter_ += 1
            return result
        # create a reference to the original method
        setattr(_flow, new_method_name, getattr(_flow, _method_name))
        # hide the original method in this instance behind the wrapper
        setattr(_flow, _method_name, new.instancemethod(wrapper, _flow))
        _flow._snapshot_instance_methods_.append(_method_name)
        _flow._snapshot_instance_methods_.append(new_method_name)
    pickle_wrap_method(flow, "_stop_training_hook")
    ### wrap __getstate__ to enable pickling ###
    # note that in the pickled flow no trace of the wrapping remains
    def wrapped_biflow_getstate(self):
        result = self.__dict__.copy()
        # delete all instancemethods
        for method_name in self._snapshot_instance_methods_:
            del result[method_name]
        # delete the special attributes which were inserted by the wrapper
        # (not really necessary)
        del result["_snapshot_counter_"]
        del result["_snapshot_path_"]
        del result["_snapshot_name_"]
        del result["_snapshot_instance_methods_"]
        # remove data attributes (generators cannot be pickled)
        # pop with default value also works when key is not present in dict
        result.pop("_train_data_iters", None)
        result.pop("_train_data_iter", None)
        result.pop("_train_msg_iters", None)
        result.pop("_train_msg_iter", None)
        result.pop("_stop_messages", None)
        result.pop("_exec_data_iter", None)
        result.pop("_exec_msg_iter", None)
        return result
    flow.__getstate__ = new.instancemethod(wrapped_biflow_getstate, flow)
    flow._snapshot_instance_methods_.append("__getstate__")
    
def remove_inspection_residues(flow):
    """Remove all the changes made by prepare_training_inspection."""
    try:
        for method_name in flow._snapshot_instance_methods_:
            delattr(flow, method_name)
        del flow._snapshot_counter_
        del flow._snapshot_path_
        del flow._snapshot_name_
        del flow._snapshot_instance_methods_
    except:
        # probably the hooks were already removed, so do nothing
        pass
    
## Further helper functions ##

def _trace_biflow_training(snapshot_path, inspection_path, css_filename,
                           x_samples, msg_samples=None, stop_messages=None,
                           trace_inspector=None,
                           debug=False, show_size=False):
    """Load flow snapshots and perform the inspection with the given data.
    
    The return value consists of the slide filenames and an index table (index
    of last slide of section indexed by node, phase, train and stop). If no
    snapshots were found the return value is None.
    
    snapshot_path -- Path were the flow training snapshots are stored.
    inspection_path -- Path were the slides are stored.
    css_filename -- Filename of the CSS file for the slides.
    x_samples, msg_samples -- Lists with the input data for the training trace.
    stop_messages -- The stop msg for the training trace.
    trace_inspector -- Instance of HTMLTraceInspector, can be None for
        default class.
    debug -- If True (default is False) then any exception will be
        caught and the gathered data up to that point is returned in the
        normal way. This is useful for binet debugging.
    show_size -- Show the approximate memory footprint of all nodes.
    """
    if not trace_inspector:
        trace_translator = TraceBiNetHTMLTranslator(show_size=show_size)
        trace_inspector = HTMLTraceInspector(
                                trace_translator=trace_translator,
                                css_filename=css_filename)
    i_train_node = 0  # index of the training node
    i_snapshot = 0 # snapshot counter
    index_table = [[]]  # last slide indexed by [node, phase, train 0 or stop 1]
    slide_filenames = []
    try:
        # search for the snapshot files
        for file_path, dirs, files in os.walk(os.path.abspath(snapshot_path)):
            dirs.sort()
            files = fnmatch.filter(files, SNAPSHOT_FILENAME + "*" + PICKLE_EXT)
            files.sort()
            for filename in files:
                filename = os.path.join(file_path, filename)
                # load the flow snapshot
                pickle_file = open(filename, "rb")
                biflow = None  # free memory
                try:
                    biflow = pickle.load(pickle_file)
                finally:
                    pickle_file.close()
                # determine which node is training and set the indices
                for node in biflow[i_train_node:]:
                    if node.get_remaining_train_phase() > 0:
                        break
                    else:
                        i_train_node += 1
                        index_table.append([])
                # inspect the training
                x = x_samples[i_train_node]
                if msg_samples:
                    msg = msg_samples[i_train_node]
                else:
                    msg = None
                if stop_messages:
                    stop_msg = stop_messages[i_train_node]
                else:
                    stop_msg = None
                trace_name = "%d_%d" % (i_snapshot, i_train_node)
                train_files, stop_files = trace_inspector.trace_training(
                                            trace_name=trace_name,
                                            path=inspection_path,
                                            flow=biflow,
                                            x=x, msg=msg, stop_msg=stop_msg,
                                            debug=debug)
                slide_filenames += train_files
                train_index = len(slide_filenames) - 1
                slide_filenames += stop_files
                stop_index = len(slide_filenames) - 1
                index_table[i_train_node].append((train_index, stop_index))
                print "got traces for snapshot %d" % (i_snapshot + 1)
                i_snapshot += 1
    except BiNetTraceDebugException, debug_exception:
        train_files, stop_files = debug_exception.result
        slide_filenames += train_files
        train_index = len(slide_filenames) - 1
        slide_filenames += stop_files
        stop_index = len(slide_filenames) - 1
        index_table[i_train_node].append((train_index, stop_index))
        debug_exception.result = index_table
        raise debug_exception
    if i_snapshot == 0:
        return None  # no snapshots were found
    return slide_filenames, index_table
